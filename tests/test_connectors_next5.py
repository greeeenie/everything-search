"""Mocked transport coverage for the second group of native marketplaces."""

from decimal import Decimal
import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from research_v2.connectors.base import ConnectorTransport
from research_v2.connectors.citilink import CitilinkConnector
from research_v2.connectors.detmir import DetmirConnector
from research_v2.connectors.dns import DnsConnector
from research_v2.connectors.lamoda import LamodaConnector
from research_v2.connectors.megamarket import MegamarketConnector
from research_v2.network import AccessError


MODEL = "Acme X2"
TITLE = "Acme X2, Special Edition"


@pytest.fixture
def transport():
    return AsyncMock(spec=ConnectorTransport)


def script(data, attr='type="application/ld+json"'):
    return f"<script {attr}>{json.dumps(data, ensure_ascii=False)}</script>"


def json_product(url=None, **other):
    return {
        "@type": "Product", "name": TITLE, "url": url,
        "offers": {"@type": "Offer", "price": "2999", "priceCurrency": "RUB", "availability": "https://schema.org/InStock"},
        **other,
    }


def captured(url, data, status=200):
    return {"url": url, "status": status, "data": data}


def assert_outcome(result, source, *, status="ok", count=1, fallback=False, error=None):
    assert result.outcome.keys() == {
        "source", "status", "offers_returned", "elapsed_ms", "detail", "error_code", "fallback_used", "warnings", "observed_at",
    }
    assert result.outcome["source"] == source
    assert result.outcome["status"] == status
    assert result.outcome["offers_returned"] == count == len(result.offers)
    assert result.outcome["elapsed_ms"] >= 0
    assert result.outcome["fallback_used"] is fallback
    assert result.outcome["error_code"] == error


def dns_tile(sku, title):
    return f"""<div class="catalog-product" data-code="{sku}">
      <a class="catalog-product__name" href="/product/{sku}/acme/">{title}</a>
      <div class="product-buy__price">2&nbsp;999 ₽ <span class="product-buy__prev">3 299 ₽</span></div>
      <button class="buy-btn">Купить</button><span class="catalog-product__rating">4,8</span>
      <a class="catalog-product__reviews-count">123 отзыва</a><img data-src="https://img.dns-shop.ru/product.png">
    </div>"""


async def test_dns_search_dom_tiles(transport):
    transport.cdp_fetch.return_value = {"html": dns_tile("777", TITLE), "http_status": 200}
    result = await DnsConnector(transport).search("Acme X2 & C++", model=MODEL)
    assert_outcome(result, "dns")
    offer = result.offers[0]
    assert (offer.sku, offer.price, offer.original_price) == ("777", Decimal(2999), Decimal(3299))
    assert (offer.availability, offer.rating, offer.reviews_count) == ("in_stock", 4.8, 123)
    assert offer.image_url == "https://img.dns-shop.ru/product.png"
    transport.cdp_fetch.assert_awaited_once_with(
        "https://www.dns-shop.ru/search/?q=Acme+X2+%26+C%2B%2B",
        wait_selector='.catalog-product, [data-id="product"], .product-buy__price',
        wait_ms=4000, wait_for_challenge=True, scroll_steps=1,
        capture_json_patterns=["ajax-state/product-buy", "ajax-state/avails-container"],
        timeout_s=40,
    )


@pytest.mark.parametrize("availability", ["now", "today", "tomorrow", "later", "out_of_stock"])
@pytest.mark.parametrize("dom_title", ["", "Наушники Sony WH-1000XM5"])
async def test_dns_merges_product_buy_state_with_catalog_card(transport, availability, dom_title):
    model = "Sony WH-1000XM5"
    json_title = "Беспроводные/проводные наушники Sony WH-1000XM5 черный 2023"
    product_id = "189ecc3c-070a-11ed-8fee-00155d8ed20b"
    path = "/product/189ecc3c070aed20/besprovodnye-nausniki-sony-wh-1000xm5/"
    transport.cdp_fetch.return_value = {
        "http_status": 200,
        "final_url": "https://www.dns-shop.ru/catalog/17a8a01d16404e77/naushniki/",
        "html": f"""
        <div class="catalog-product" data-product="{product_id}" data-code="5057953"
             data-avail-status="{availability}">
          <a class="catalog-product__name" href="{path}">{dom_title}</a>
          <span class="catalog-product__rating">4,8</span>
          <a class="catalog-product__reviews-count">123 отзыва</a>
        </div>""",
        "captured_json": [
            captured("https://www.dns-shop.ru/ajax-state/product-buy/", {"result": True, "data": {"states": [
                {"id": "as-83XRvL", "data": {
                    "id": product_id, "name": json_title, "price": {"current": 25299, "previous": 29999},
                }},
            ]}}),
        ],
    }
    result = await DnsConnector(transport).search(model, model=model)
    if availability == "out_of_stock":
        assert_outcome(result, "dns", count=0)
        return
    assert_outcome(result, "dns")
    offer = result.offers[0]
    assert (offer.price, offer.original_price, offer.title) == (25299, 29999, dom_title or json_title)
    assert (offer.sku, offer.rating, offer.reviews_count, offer.availability) == ("5057953", 4.8, 123, "in_stock")
    assert offer.url == "https://www.dns-shop.ru" + path


async def test_dns_search_redirect_to_product(transport):
    url = "https://www.dns-shop.ru/product/240d90b/acme-x2/"
    transport.cdp_fetch.return_value = {
        "final_url": url,
        "html": f'<h1>{TITLE}</h1><div class="product-buy__price">3 299 ₽</div><span>В наличии</span>',
    }
    result = await DnsConnector(transport).search("240d90b", model=MODEL)
    assert_outcome(result, "dns")
    assert result.offers[0].url == url
    assert result.offers[0].sku == "240d90b"


async def test_dns_card_merges_structured_and_dom(transport):
    path = "/product/e873f/acme-x2/"
    html = script(json_product(path, sku="001234", aggregateRating={"ratingValue": 4.6, "reviewCount": 9})) + f"""
      <main><h1>{TITLE}</h1><section class="product-characteristics__spec">
        <span class="product-characteristics__spec-title">Вес</span>
        <span class="product-characteristics__spec-value">1.5 кг</span>
      </section></main>"""
    transport.cdp_fetch.return_value = {"html": html}
    result = await DnsConnector(transport).get_card(path, model=MODEL)
    assert result.error is None
    assert result.products[0].specifications == {"Вес": "1.5 кг"}
    assert (result.offers[0].sku, result.offers[0].rating, result.offers[0].price) == ("001234", 4.6, 2999)
    transport.cdp_fetch.assert_awaited_once_with(
        "https://www.dns-shop.ru" + path,
        wait_selector="h1, .product-buy__price", wait_for_challenge=True, timeout_s=40,
    )


async def test_dns_search_jsonld_item_list_precedes_dom(transport):
    transport.cdp_fetch.return_value = {"html": script({
        "@type": "ItemList", "itemListElement": [{
            "@type": "ListItem", "url": "/product/444/acme/",
            "item": json_product(offers={"price": 4999, "availability": "InStock"}),
        }],
    }) + dns_tile("444", TITLE)}
    result = await DnsConnector(transport).search(MODEL)
    assert_outcome(result, "dns")
    assert result.offers[0].price == 4999
    assert result.offers[0].rating == 4.8


@pytest.mark.parametrize(("capture", "status", "code"), [
    ({"html": "<title>Qrator DDoS Protection</title>", "http_status": 200}, "blocked", "captcha_or_access_challenge"),
    ({"error": "challenge_unresolved"}, "blocked", "challenge_unresolved"),
    (AccessError("browser_timeout"), "timeout", "browser_timeout"),
    ({"html": "<h1>Service unavailable</h1>", "http_status": 503}, "error", "http_503"),
])
async def test_dns_search_capture_failure(transport, capture, status, code):
    if isinstance(capture, AccessError):
        transport.cdp_fetch.side_effect = capture
    else:
        transport.cdp_fetch.return_value = capture
    result = await DnsConnector(transport).search(MODEL)
    assert_outcome(result, "dns", status=status, count=0, error=code)
    assert result.outcome["detail"] == code
    assert result.errors == [{"source": "dns", "error": code}]


def citilink_product(sku, title):
    return {
        "productId": sku, "title": title, "slug": f"acme-x2-{sku}", "price": 2999, "priceClub": 2799,
        "oldPrice": 3299, "isAvailable": True, "rating": 4.7, "reviewsCount": 18,
    }


def citilink_next(*items):
    return script({"props": {"pageProps": {"searchResult": {"products": items}}}}, 'id="__NEXT_DATA__" type="application/json"')


async def test_citilink_search_next_data(transport):
    transport.cdp_fetch.return_value = {"html": citilink_next(citilink_product(222, TITLE))}
    result = await CitilinkConnector(transport).search(MODEL)
    assert_outcome(result, "citilink")
    offer = result.offers[0]
    assert offer.url == "https://www.citilink.ru/product/acme-x2-222/"
    assert (offer.sku, offer.price, offer.conditional_price, offer.original_price) == ("222", 2999, 2799, 3299)
    assert (offer.rating, offer.reviews_count) == (4.7, 18)
    assert offer.price_conditions == ["club_membership"]
    transport.cdp_fetch.assert_awaited_once_with(
        "https://www.citilink.ru/search/?text=Acme+X2", wait_selector='a[href*="/product/"], [data-meta-product-id]',
        wait_ms=3500, wait_for_challenge=True, scroll_steps=1, capture_json_patterns=["graphql", "search"], timeout_s=40,
    )


async def test_citilink_search_dom_marked_and_generic(transport):
    transport.cdp_fetch.return_value = {"html": f"""
      <script id="__NEXT_DATA__">{{broken</script>
      <article data-meta-name="SnippetProductVerticalLayout" data-meta-product-id="222">
        <a href="/product/acme-x2-222/">{TITLE}</a>
        <span data-meta-price="2999">2 999 ₽</span><del>3 299 ₽</del>
        <span class="rating">4,5</span><img src="//cdn.citilink.ru/acme.jpg"><button>В корзину</button>
      </article>
      <div><a href="/product/acme-x2-223/">{TITLE}</a><span>1 999 ₽</span><s>2 499 ₽</s><p>В наличии</p></div>
    """}
    result = await CitilinkConnector(transport).search(MODEL)
    assert_outcome(result, "citilink", count=2)
    first, second = result.offers
    assert (first.sku, first.price, first.original_price, first.rating) == ("222", 2999, 3299, 4.5)
    assert first.image_url == "https://cdn.citilink.ru/acme.jpg"
    assert (second.price, second.original_price, second.rating) == (1999, 2499, None)


async def test_citilink_card_uses_matching_next_listing_and_jsonld(transport):
    path = "/product/acme-x2-222/"
    transport.cdp_fetch.return_value = {"html": script(json_product(path, brand={"name": "Acme"})) + citilink_next(
        citilink_product(223, "Acme X2 Pro"), citilink_product(222, TITLE),
    )}
    result = await CitilinkConnector(transport).get_card(path, model=MODEL)
    assert result.error is None
    assert len(result.offers) == 1
    assert (result.products[0].brand, result.offers[0].sku, result.offers[0].rating) == ("Acme", "222", 4.7)
    mismatch = await CitilinkConnector(transport).get_card(path, model="Acme X2 Pro")
    assert mismatch.error == "model_mismatch" and not mismatch.offers


def mega_product(sku, title):
    return {
        "goods": {"title": title, "webUrl": f"/catalog/details/acme-{sku}/", "goodsId": sku,
                  "brand": {"name": "Acme"}, "image": "https://static.megamarket.ru/acme.png"},
        "finalPrice": 2999, "price": 3299, "isAvailable": True, "rating": 4.9, "reviewCount": 37,
        "merchantName": "Техника Маркет",
    }


def mega_capture(*items):
    return captured("https://megamarket.ru/api/catalogService/catalog/search", {"data": {"items": list(items)}})


async def test_megamarket_search_uses_captured_goods(transport):
    transport.cdp_fetch.return_value = {"captured_json": [mega_capture(mega_product("1000000222", TITLE))]}
    result = await MegamarketConnector(transport).search(MODEL)
    assert_outcome(result, "megamarket")
    offer = result.offers[0]
    assert (offer.sku, offer.price, offer.original_price, offer.rating, offer.reviews_count) == ("1000000222", 2999, 3299, 4.9, 37)
    assert offer.seller == "Техника Маркет"
    assert result.products[0].brand == "Acme"
    transport.cdp_fetch.assert_awaited_once_with(
        "https://megamarket.ru/catalog/?q=Acme+X2", wait_selector='[data-test="product-item"], a[href*="/catalog/details/"]',
        wait_ms=3500, wait_for_challenge=True, scroll_steps=1,
        capture_json_patterns=["catalogService/catalog/search", "search"], timeout_s=40,
    )


async def test_megamarket_search_dom_with_bonus(transport):
    transport.cdp_fetch.return_value = {"html": f"""
      <div data-test="product-item" data-goods-id="1000000222">
        <a href="/catalog/details/acme-1000000222/">{TITLE}</a>
        <span data-test="price">2 999 ₽</span><span data-test="old-price">3 299 ₽</span>
        <span data-test="cashback">300 бонусов</span><span data-test="seller">Техника Маркет</span>
        <span data-test="rating">4,9</span><button>В корзину</button>
      </div>"""}
    result = await MegamarketConnector(transport).search(MODEL)
    assert_outcome(result, "megamarket")
    assert result.outcome["fallback_used"] is False
    offer = result.offers[0]
    assert (offer.price, offer.original_price, offer.rating, offer.seller) == (2999, 3299, 4.9, "Техника Маркет")
    assert offer.price_conditions == ["300 бонусов"]


async def test_megamarket_card_id_redirect_and_related_product(transport):
    url = "https://megamarket.ru/catalog/details/acme-1000000222/"
    transport.cdp_fetch.return_value = {
        "html": script(json_product(url, additionalProperty={"name": "Вес", "value": "1.5 кг"})),
        "final_url": url,
        "captured_json": [mega_capture(mega_product("1000000444", "Acme X4"), mega_product("1000000222", TITLE))],
    }
    result = await MegamarketConnector(transport).get_card("1000000222", model=MODEL)
    assert result.error is None and [offer.url for offer in result.offers] == [url]
    assert result.products[0].specifications == {"Вес": "1.5 кг"}
    assert result.offers[0].seller == "Техника Маркет"
    assert transport.cdp_fetch.await_args.args == ("https://megamarket.ru/catalog/details/1000000222/",)


def lamoda_tile(sku, title, button="<button>В корзину</button>"):
    return f"""<a class="x-product-card__card" href="/p/{sku}/collection-shoes/">
      <span class="x-product-card-description__brand-name">Acme</span>
      <span class="x-product-card-description__product-name">{title}</span>
      <span class="x-product-card-description__price-new">2 999 ₽</span>
      <span class="x-product-card-description__price-old">3 299 ₽</span>
      <img src="https://a.lmcdn.ru/img/acme.jpg">{button}
    </a>"""


def lamoda_product(sku, title):
    return {"sku": sku, "name": title, "brand": {"name": "Acme"}, "price_amount": 2999,
            "old_price_amount": 3299, "seo_tail": "/shoes/acme/", "is_available": True}


async def test_lamoda_search_dom(transport):
    transport.cdp_fetch.return_value = {"html": lamoda_tile("MP002XM12345", "X2, Special Edition")}
    result = await LamodaConnector(transport).search(MODEL, model=MODEL)
    assert_outcome(result, "lamoda")
    offer = result.offers[0]
    assert (offer.url, offer.sku) == ("https://www.lamoda.ru/p/MP002XM12345/", "MP002XM12345")
    assert (offer.price, offer.original_price, offer.rating) == (2999, 3299, None)
    assert result.products[0].brand == "Acme"
    transport.cdp_fetch.assert_awaited_once_with(
        "https://www.lamoda.ru/catalogsearch/result/?q=Acme+X2", wait_selector='a[href*="/p/"], .x-product-card__card',
        wait_ms=3500, wait_for_challenge=True, scroll_steps=1,
        capture_json_patterns=["catalog", "search", "graphql"], timeout_s=40,
    )


@pytest.mark.parametrize("state", [
    script({"products": [lamoda_product("MP002XM12345", TITLE)]}, 'id="__NUXT__"'),
    "<script>window.__NUXT__=" + json.dumps({"products": [lamoda_product("MP002XM12345", TITLE)]}) + ";</script>",
    script([{"data": 1}, ["Reactive", 2], {"products": 3}, [4], {
        "sku": 5, "name": 6, "brand": 7, "price_amount": 9, "is_available": 10,
    }, "MP002XM12345", TITLE, {"name": 8}, "Acme", 2999, True], 'id="__NUXT_DATA__"'),
])
async def test_lamoda_search_embedded_state(transport, state):
    transport.cdp_fetch.return_value = {"html": state + lamoda_tile("mp002xm12345", TITLE)}
    result = await LamodaConnector(transport).search(MODEL)
    assert_outcome(result, "lamoda")
    assert result.offers[0].price == 2999
    assert result.offers[0].image_url == "https://a.lmcdn.ru/img/acme.jpg"


async def test_lamoda_card_sku_and_size_variants(transport):
    transport.cdp_fetch.return_value = {"html": script(json_product(brand={"name": "Acme"})) + f"""
      <main><h1>{TITLE}</h1><button data-size="39">39</button><button data-size="40">40</button>
      <button data-size="39">39</button></main>"""}
    result = await LamodaConnector(transport).get_card("MP002XM12345", model=MODEL)
    assert result.error is None and result.products[0].variants == ["39", "40"]
    assert (result.offers[0].variants, result.offers[0].availability) == ({"sizes": "39 | 40"}, "in_stock")
    assert transport.cdp_fetch.await_args.args == ("https://www.lamoda.ru/p/MP002XM12345/",)


API_URL = "https://api.detmir.ru/v2/products"


def detmir_product(sku, title):
    return {
        "id": sku, "title": title, "brand": {"title": "Acme"}, "price": {"price": 2999, "currency": "RUB"},
        "old_price": {"price": 3299}, "available": {"online": {"warehouse_codes": ["MSK"]}},
        "rating": 4.6, "review_count": 52, "pictures": [{"original": "https://static.detmir.ru/acme.png"}],
        "vendor": {"title": "Детские товары"}, "characteristics": [{"title": "Цвет", "value": "Зелёный"}],
    }


@pytest.mark.parametrize("wrapped", [False, True])
async def test_detmir_search_from_public_api(transport, wrapped):
    data = [detmir_product(222, TITLE)]
    transport.http_get.return_value = httpx.Response(200, json={"items": data} if wrapped else data)
    result = await DetmirConnector(transport).search("Acme X2 & другая вещь", model=MODEL, limit=20)
    assert_outcome(result, "detsky_mir")
    offer = result.offers[0]
    assert (offer.sku, offer.price, offer.original_price, offer.rating, offer.reviews_count) == ("222", 2999, 3299, 4.6, 52)
    assert (offer.image_url, offer.seller) == ("https://static.detmir.ru/acme.png", "Детские товары")
    assert result.products[0].brand == "Acme"
    transport.http_get.assert_awaited_once_with(API_URL, params={
        "filter": "site:detmir;search:Acme X2 & другая вещь;region.iso:RU-MOW", "limit": 30, "offset": 0, "meta": "*",
    }, headers={"Accept": "application/json"})
    transport.cdp_fetch.assert_not_awaited()


async def test_detmir_card_from_public_api(transport):
    transport.http_get.return_value = httpx.Response(200, json=detmir_product(222, TITLE))
    result = await DetmirConnector(transport).get_card("https://www.detmir.ru/product/index/id/222/", model=MODEL)
    assert result.error is None and result.products[0].specifications == {"Цвет": "Зелёный"}
    assert result.offers[0].availability == "in_stock"
    transport.http_get.assert_awaited_once_with(API_URL + "/222", params={"meta": "*"}, headers={"Accept": "application/json"})
    transport.cdp_fetch.assert_not_awaited()


@pytest.mark.parametrize("source", ["capture", "dom"])
async def test_detmir_search_falls_back_after_api_403(transport, source):
    transport.http_get.return_value = httpx.Response(403)
    transport.cdp_fetch.return_value = (
        {"captured_json": [captured(API_URL + "?meta=*", {"items": [detmir_product(222, TITLE)]})]} if source == "capture"
        else {"html": f"""<article><a href="/product/index/id/222/">{TITLE}</a>
                           <span data-test="price">2 999 ₽</span><span>В наличии</span></article>"""}
    )
    result = await DetmirConnector(transport).search(MODEL)
    assert_outcome(result, "detsky_mir", fallback=True)
    assert result.outcome["warnings"] == ["api_http_403"]
    url = urlsplit(transport.cdp_fetch.await_args.args[0])
    assert (url.netloc, url.path, parse_qs(url.query)) == ("www.detmir.ru", "/search/results/", {"qt": [MODEL]})
    assert transport.cdp_fetch.await_args.kwargs == {
        "wait_selector": 'a[href*="/product/index/id/"]', "wait_ms": 3000,
        "capture_json_patterns": ["api.detmir.ru/v2/products"], "timeout_s": 35,
    }


async def test_detmir_card_falls_back_after_api_failure(transport):
    transport.http_get.return_value = httpx.Response(429)
    url = "https://www.detmir.ru/product/index/id/222/"
    transport.cdp_fetch.return_value = {"final_url": url, "captured_json": [captured(API_URL + "/222", detmir_product(222, TITLE))]}
    result = await DetmirConnector(transport).get_card("222")
    assert result.error is None and result.offers[0].price == 2999
    assert transport.cdp_fetch.await_args.args == (url,)


async def test_detmir_both_transports_fail(transport):
    transport.http_get.side_effect = AccessError("timeout")
    transport.cdp_fetch.return_value = {"error": "challenge_unresolved"}
    result = await DetmirConnector(transport).search(MODEL)
    assert_outcome(result, "detsky_mir", status="blocked", count=0, fallback=True, error="challenge_unresolved")
    assert result.outcome["warnings"] == ["api_timeout"]


@pytest.mark.parametrize("connector", [DnsConnector, CitilinkConnector, MegamarketConnector, LamodaConnector, DetmirConnector])
async def test_exact_filter_rejects_similar_models_and_accessories(transport, connector):
    titles = ["Acme X20", "Acme X2 Pro", "Чехол для Acme X2", TITLE]
    if connector is DnsConnector:
        transport.cdp_fetch.return_value = {"html": "".join(dns_tile(str(i), title) for i, title in enumerate(titles))}
    elif connector is CitilinkConnector:
        transport.cdp_fetch.return_value = {"html": citilink_next(*(citilink_product(i + 1, title) for i, title in enumerate(titles)))}
    elif connector is MegamarketConnector:
        transport.cdp_fetch.return_value = {"captured_json": [mega_capture(*(mega_product(i + 1, title) for i, title in enumerate(titles)))]}
    elif connector is LamodaConnector:
        transport.cdp_fetch.return_value = {"html": "".join(lamoda_tile(f"MP002XM{i}", title) for i, title in enumerate(titles))}
    else:
        transport.http_get.return_value = httpx.Response(200, json=[detmir_product(i + 1, title) for i, title in enumerate(titles)])
    result = await connector(transport).search(MODEL, model=MODEL, limit=1)
    assert_outcome(result, connector.name)
    assert result.offers[0].title == TITLE
    assert result.products[0].model == MODEL


async def test_unknown_values_stay_unknown_and_search_excludes_those_offers(transport):
    connector = LamodaConnector(transport)
    transport.cdp_fetch.return_value = {"html": lamoda_tile("MP002XM12345", TITLE, "<button disabled>В корзину</button>")}
    search = await connector.search(MODEL)
    assert_outcome(search, "lamoda", count=0)
    transport.cdp_fetch.return_value = {"html": f"<h1>{TITLE}</h1><button disabled>В корзину</button>"}
    card = await connector.get_card("MP002XM12345")
    offer = card.offers[0]
    assert (offer.price, offer.availability, offer.rating, offer.reviews_count) == (None, "unknown", None, None)


async def test_card_does_not_borrow_price_or_stock_from_related_tile(transport):
    transport.cdp_fetch.return_value = {"html": f"<main><h1>{TITLE}</h1>{dns_tile('123', 'Acme X4')}</main>"}
    result = await DnsConnector(transport).get_card("/product/222/acme/")
    assert result.error is None
    assert (result.offers[0].price, result.offers[0].availability, result.offers[0].rating) == (None, "unknown", None)


@pytest.mark.parametrize("connector", [DnsConnector, CitilinkConnector, MegamarketConnector, LamodaConnector, DetmirConnector])
async def test_cards_reject_foreign_urls_before_fetch(transport, connector):
    result = await connector(transport).get_card("https://foreign.example/product/index/id/222/")
    assert result.error == "invalid_product_url"
    transport.cdp_fetch.assert_not_awaited()
    transport.http_get.assert_not_awaited()
