"""Native connector contracts with recorded-shape responses and no live transport."""

from decimal import Decimal
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from research_v2.connectors.aliexpress import AliExpressConnector
from research_v2.connectors.avito import AvitoConnector
from research_v2.connectors.base import ConnectorTransport
from research_v2.connectors.ozon import OzonConnector
from research_v2.connectors.wildberries import WildberriesConnector
from research_v2.connectors.yandex_market import YandexMarketConnector
from research_v2.network import AccessError

MODEL = "Wanbo Vali 1 Pro"
WRONG_MODEL = "Wanbo ToGo Pro"


@pytest.fixture
def transport():
    mock = Mock(spec=ConnectorTransport)
    for name in ("http_get", "cdp_fetch", "camoufox_fetch"):
        setattr(mock, name, AsyncMock(side_effect=AssertionError(f"Unexpected {name} call")))
    return mock


def returns(mock, *responses):
    mock.side_effect = list(responses)


def browser(html="", *, payload=None, status=200, final_url=None):
    result = {"html": html, "http_status": status}
    if final_url:
        result["final_url"] = final_url
    if payload is not None:
        result["captured_json"] = [{"url": "https://example.com/marketplace-api", "status": 200, "data": payload}]
    return result


def ld(payload):
    return f'<script type="application/ld+json">{json.dumps(payload, ensure_ascii=False)}</script>'


def schema_product(title=MODEL, *, price=27000, url=None):
    return {
        "@type": "Product", "name": title, "brand": {"name": "Wanbo"}, "url": url,
        "aggregateRating": {"ratingValue": "4.8", "reviewCount": 24},
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "RUB", "availability": "https://schema.org/InStock"},
    }


def wb_item(sku=111222333, title=MODEL, **fields):
    return {
        "id": sku, "name": title, "brand": "Wanbo", "supplier": "Проектор Маркет",
        "reviewRating": 4.8, "feedbacks": 47, "totalQuantity": 18,
        "sizes": [{"price": {"product": 2345678, "basic": 3000000}, "stocks": [{"qty": 18}]}],
        **fields,
    }


async def test_wildberries_http_search_uses_kopecks_and_observed_quantity(transport):
    data = {"data": {"products": [wb_item(), wb_item(222333444, totalQuantity=0), wb_item()]}}
    returns(transport.http_get, httpx.Response(200, json=data))
    result = await WildberriesConnector(transport).search("Проектор Wanbo", model=MODEL)
    assert len(result.products) == len(result.offers) == 1
    offer = result.offers[0]
    assert offer.price == Decimal("23456.78") and offer.original_price == Decimal("30000")
    assert (offer.seller, offer.rating, offer.reviews_count) == ("Проектор Маркет", 4.8, 47)
    assert offer.availability == "in_stock"
    assert offer.url == "https://www.wildberries.ru/catalog/111222333/detail.aspx"
    assert result.outcome["status"] == "ok" and result.outcome["offers_returned"] == 1
    assert result.outcome["elapsed_ms"] >= 0 and not result.outcome["fallback_used"]
    assert transport.http_get.call_args.args[0] == "https://search.wb.ru/exactmatch/ru/common/v9/search"
    assert transport.http_get.call_args.kwargs["params"] == {
        "appType": 1, "curr": "rub", "dest": -1257786, "spp": 30, "ab_testing": "false",
        "query": "Проектор Wanbo", "resultset": "catalog", "sort": "popular", "suppressSpellcheck": "false",
    }


async def test_wildberries_dom_wallet_and_regular_prices(transport):
    returns(transport.http_get, httpx.Response(429))
    returns(transport.cdp_fetch, browser("""
      <article data-nm-id="111222333">
        <span class="product-card__brand">Wanbo /</span><span class="product-card__name">Vali 1 Pro</span>
        <ins class="price__lower-price">20 990 ₽</ins><del>25 000 ₽</del>
        <span class="address-rate-mini">4,9</span><span class="product-card__count">87 отзывов</span>
        <button>В корзину</button>
      </article>
      <div class="product-card"><a href="/catalog/222333444/detail.aspx" aria-label="Wanbo Vali 1 Pro"></a>
        <ins data-price-type="regular">21 990 ₽</ins><button>В корзину</button>
      </div>
    """))
    result = await WildberriesConnector(transport).search(MODEL, model=MODEL)
    wallet, regular = result.offers
    assert wallet.price is None and wallet.conditional_price == Decimal("20990")
    assert wallet.original_price == Decimal("25000")
    assert wallet.price_scope == "conditional" and wallet.price_conditions == ["wallet"]
    assert wallet.rating == 4.9 and wallet.reviews_count == 87
    assert regular.price == Decimal("21990") and regular.conditional_price is None
    assert result.outcome["status"] == "ok" and result.outcome["fallback_used"]
    assert "wb_dom_fallback: tile prices may reflect WB Wallet discount" in result.outcome["warnings"]
    assert "exactmatch" in transport.cdp_fetch.call_args.kwargs["capture_json_patterns"]


async def test_wildberries_prefers_captured_prices_over_tiles(transport):
    returns(transport.http_get, AccessError("ru_egress_unavailable"))
    returns(transport.cdp_fetch, browser(
        '<article data-nm-id="111222333"><b class="product-card__name">Wrong title</b><ins>1 ₽</ins></article>',
        payload={"products": [wb_item()]},
    ))
    result = await WildberriesConnector(transport).search(MODEL, model=MODEL)
    assert result.offers[0].price == Decimal("23456.78") and result.offers[0].conditional_price is None
    assert result.outcome["fallback_used"]
    assert all("wb_dom_fallback" not in warning for warning in result.outcome["warnings"])


async def test_wildberries_dom_does_not_infer_stock_from_only_a_price(transport):
    returns(transport.http_get, httpx.Response(403))
    returns(transport.cdp_fetch, browser(f"""
      <article data-nm-id="111222333"><h2 class="product-card__name">{MODEL}</h2>
        <ins>22 990 ₽</ins><button disabled>В корзину</button></article>
    """))
    result = await WildberriesConnector(transport).search(MODEL, model=MODEL)
    assert result.offers == []
    assert result.outcome["status"] == "ok" and result.outcome["offers_returned"] == 0


@pytest.mark.parametrize("imt_from_basket", [False, True])
async def test_wildberries_card_and_reviews_use_confirmed_imt_id(transport, imt_from_basket):
    async def http_get(url, **kwargs):
        if "search.wb.ru" in url:
            return httpx.Response(200, json={"products": [wb_item()]})
        if "card.wb.ru" in url:
            return httpx.Response(200, json={"products": [wb_item(root=None if imt_from_basket else 4123)]})
        if url.endswith("/vol1112/part111222/111222333/info/ru/card.json"):
            return httpx.Response(200, json={
                "nm_id": 111222333, "imt_id": 4123 if imt_from_basket else 9999,
                "options": [{"name": "Разрешение", "value": "1920×1080"}],
            })
        if url == "https://feedbacks2.wb.ru/feedbacks/v1/4123":
            return httpx.Response(429)
        if url == "https://feedbacks1.wb.ru/feedbacks/v1/4123":
            return httpx.Response(200, json={"feedbacks": [
                {"text": "Работает", "pros": "Яркий", "productValuation": 5, "createdDate": "2026-11-22"},
                {"text": "Проблемы с фокусом", "cons": "Размыто", "productValuation": 1, "createdDate": "2026-10-25"},
                {"text": "Удобный", "productValuation": 5, "createdDate": "2026-10-01"},
            ]})
        raise AssertionError(url)

    transport.http_get.side_effect = http_get
    connector = WildberriesConnector(transport)
    card = await connector.get_card("111222333", model=MODEL)
    assert card.error is None and card.products[0].specifications == {"Разрешение": "1920×1080"}
    assert card.offers[0].price == Decimal("23456.78")
    reviews, errors = await connector.get_reviews(MODEL, limit=2)
    assert not errors and [review["rating"] for review in reviews] == [1, 5]
    assert reviews[0]["cons"] == "Размыто" and reviews[1]["pros"] == "Яркий"
    assert all(review["url"] == card.offers[0].url and review["marketplace"] == "wildberries" for review in reviews)


async def test_wildberries_card_browser_fallback(transport):
    async def http_get(url, **kwargs):
        return httpx.Response(403 if "card.wb.ru" in url else 404)

    transport.http_get.side_effect = http_get
    returns(transport.cdp_fetch, browser("<h1>Wanbo Vali 1 Pro</h1>", payload={"products": [wb_item()]}))
    result = await WildberriesConnector(transport).get_card("https://www.wildberries.ru/catalog/111222333/detail.aspx")
    assert result.error is None and result.offers[0].price == Decimal("23456.78")
    assert "card.json" in transport.cdp_fetch.call_args.kwargs["capture_json_patterns"]


@pytest.mark.parametrize("search_root", [None, 4123])
async def test_wildberries_reviews_resolve_search_root_or_basket_when_card_is_blocked(transport, search_root):
    async def http_get(url, **kwargs):
        if "search.wb.ru" in url or "card.wb.ru" in url:
            return httpx.Response(403)
        if url == "https://basket-07.wbbasket.ru/vol1112/part111222/111222333/info/ru/card.json":
            return httpx.Response(403)
        if url == "https://basket-06.wbbasket.ru/vol1112/part111222/111222333/info/ru/card.json":
            return httpx.Response(200, json={
                "imt_id": 4123, "subj_name": "Проекторы", "options": [{"name": "Разрешение", "value": "1920×1080"}],
            })
        if url == "https://feedbacks2.wb.ru/feedbacks/v1/4123":
            return httpx.Response(200, json={"feedbacks": [{"text": "Фокус точный", "productValuation": 5}]})
        raise AssertionError(url)

    transport.http_get.side_effect = http_get
    capture = browser(payload={"data": {"products": [wb_item(root=search_root)]}})
    capture["captured_json"][0]["url"] = "https://www.wildberries.ru/__internal/u-search/exactmatch"
    returns(transport.cdp_fetch, capture)
    reviews, errors = await WildberriesConnector(transport).get_reviews(MODEL)
    assert not errors and [(review["text"], review["rating"]) for review in reviews] == [("Фокус точный", 5)]
    resolved_via_card = any("card.wb.ru" in call.args[0] for call in transport.http_get.await_args_list)
    assert resolved_via_card is (search_root is None)


def ozon_widget(title=MODEL):
    return {"widgetStates": {"tileGridDesktop-1030-default-1": json.dumps({"items": [{
        "action": {"link": "/product/wanbo-1923456789/?avtc=1"}, "isAvailable": True,
        "mainState": [
            {"id": "price", "priceV2": {"price": [
                {"text": "24 525 ₽", "textStyle": "PRIMARY"},
                {"text": "27 000 ₽", "textStyle": "SECONDARY"},
                {"text": "32 000 ₽", "textStyle": "ORIGINAL"},
            ]}},
            {"id": "name", "textAtom": {"text": title}},
            {"labelList": {"items": [{"title": {"text": "4,9"}}, {"title": {"text": "123 отзыва"}}]}},
        ],
    }]})}}


@pytest.mark.parametrize("blocked", [False, True])
async def test_ozon_widgets_separate_card_and_public_prices(transport, blocked):
    if blocked:
        returns(transport.cdp_fetch, {"error": "http_403", "http_status": 403})
        returns(transport.camoufox_fetch, browser(payload=ozon_widget()))
    else:
        returns(transport.cdp_fetch, browser(payload=ozon_widget()))
    result = await OzonConnector(transport).search(MODEL, model=MODEL)
    assert len(result.offers) == 1
    offer = result.offers[0]
    assert (offer.price, offer.conditional_price, offer.original_price) == (27000, 24525, 32000)
    assert offer.price_conditions == ["bank_card"] and offer.price_scope == "public"
    assert offer.rating == 4.9 and offer.reviews_count == 123 and offer.sku == "1923456789"
    assert result.outcome["fallback_used"] is blocked and result.outcome["status"] == "ok"
    assert transport.cdp_fetch.call_args.kwargs["user_agent_compat"] == "windows"
    if blocked:
        assert transport.camoufox_fetch.call_args.kwargs["allow_search"] is True


async def test_ozon_itemlist_and_review_fallback(transport):
    listing_url = "https://www.ozon.ru/product/wanbo-1923456789/"
    item_list = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "url": listing_url, "item": schema_product()},
    ]}
    review = {"@type": "Review", "reviewBody": "Фокус точный", "reviewRating": {"ratingValue": "5"}}
    returns(transport.cdp_fetch, browser(ld(item_list)), {"error": "challenge_unresolved"})
    returns(transport.camoufox_fetch, browser(ld({**schema_product(), "review": [review]})))
    reviews, errors = await OzonConnector(transport).get_reviews(MODEL)
    assert not errors and reviews[0]["text"] == "Фокус точный" and reviews[0]["rating"] == 5
    assert reviews[0]["marketplace"] == "ozon" and reviews[0]["url"] == listing_url
    assert transport.camoufox_fetch.call_args.args[0] == listing_url + "reviews/"
    assert transport.camoufox_fetch.call_args.kwargs["allow_search"] is False


async def test_ozon_card_merges_schema_with_price_seller_and_specs_widgets(transport):
    card_state = {"widgetStates": {key: json.dumps(value) for key, value in {
        "webPrice-101": {"price": "27 200 ₽", "cardPrice": "25 100 ₽", "originalPrice": "31 100 ₽", "isAvailable": True},
        "webCurrentSeller-102": {"seller": {"name": "Wanbo Store"}},
        "webCharacteristics-103": {"characteristics": [{"title": "Модель", "value": "Vali 1 Pro"}]},
    }.items()}}
    returns(transport.cdp_fetch, {"error": "http_403"})
    returns(transport.camoufox_fetch, browser(ld(schema_product(price=25100)), payload=card_state))
    result = await OzonConnector(transport).get_card("1923456789", model=MODEL)
    assert result.error is None and result.products[0].specifications == {"Модель": "Vali 1 Pro"}
    offer = result.offers[0]
    assert (offer.price, offer.conditional_price, offer.original_price, offer.seller) == (27200, 25100, 31100, "Wanbo Store")
    assert offer.availability == "in_stock" and (offer.rating, offer.reviews_count) == (4.8, 24)
    assert transport.camoufox_fetch.call_args.kwargs["allow_search"] is False


async def test_ozon_reviews_return_source_error_when_both_transports_are_blocked(transport):
    blocked = {"error": "http_403", "http_status": 403}
    returns(transport.cdp_fetch, browser(payload=ozon_widget()), blocked, blocked)
    returns(transport.camoufox_fetch, blocked, blocked)
    reviews, errors = await OzonConnector(transport).get_reviews(MODEL)
    assert reviews == [] and errors == [{"source": "ozon", "error": "http_403", "stage": "reviews"}]


async def test_ozon_dom_preserves_listed_prices_and_excludes_sold_out_tiles(transport):
    returns(transport.cdp_fetch, browser(f"""
      <article><a href="/product/wanbo-1923456789/"><h2>{MODEL}</h2></a>
        <span data-price-type="card">25 100 ₽</span><span data-price-type="regular">27 000 ₽</span>
        <button>В корзину</button>
      </article>
      <article><a href="/product/wanbo-2923456789/"><h2>{MODEL}</h2></a><ins>900 ₽</ins>
        <span>Раскупили</span>
      </article>
    """))
    result = await OzonConnector(transport).search(MODEL, model=MODEL)
    assert [(offer.sku, offer.price, offer.conditional_price) for offer in result.offers] == [
        ("1923456789", 27000, 25100),
    ]


async def test_ozon_live_ssr_tile_uses_thin_space_prices_rating_and_reviews(transport):
    model = "Sony WH-1000XM5"
    title = model + " Наушники накладные беспроводные, серебристые"
    returns(transport.cdp_fetch, browser(f"""
      <div class="grid">
        <div class="tile">
          <div><a href="/product/sony-wh-1000xm5-1923456789/?at=image"><img src="/card.jpg"></a></div>
          <div><span>Чёрная пятница</span><span>24\u2009734\u2009₽</span><span>37\u2009990\u2009₽</span>
            <span>−34%</span><span>19\u2009ед осталось</span><span>Sony</span></div>
          <div><a href="https://www.ozon.ru/product/sony-wh-1000xm5-1923456789?at=title">{title}</a></div>
          <div><span>4.9</span><span>522</span></div><span>Ozon</span><span>2 декабря</span>
        </div>
        <div class="tile"><a href="/product/other-item-2923456789/">Другой товар</a><span>900 ₽</span></div>
      </div>
    """))
    result = await OzonConnector(transport).search(model, model=model)
    offer, = result.offers
    assert offer.title == title
    assert offer.url == "https://www.ozon.ru/product/sony-wh-1000xm5-1923456789/"
    assert (offer.price, offer.original_price, offer.conditional_price) == (24734, 37990, None)
    assert (offer.rating, offer.reviews_count, offer.availability) == (4.9, 522, "in_stock")


@pytest.mark.parametrize("prices", [
    "<span>37\u00a0990 ₽</span><span>24\u2009734 ₽ с Ozon картой</span>",
    "<span>37\u00a0990 ₽ 24\u2009734 ₽ с Ozon картой</span>",
])
async def test_ozon_dom_captures_second_price_explicitly_labelled_with_ozon_card(transport, prices):
    returns(transport.cdp_fetch, browser(f"""
      <div><a href="/product/wanbo-1923456789/">{MODEL}</a>
        {prices}
      </div>
    """))
    result = await OzonConnector(transport).search(MODEL, model=MODEL)
    offer, = result.offers
    assert (offer.price, offer.conditional_price, offer.original_price) == (37990, 24734, None)
    assert offer.price_scope == "public" and offer.price_conditions == ["bank_card"]


def yandex_tile(title=MODEL):
    return f"""
    <article data-auto="searchOrganic"><a href="/card/proektor-wanbo/101000?cpc=tracking">
      <span data-auto="snippet-title">{title}</span></a>
      <span data-auto="snippet-price-current" aria-label="Цена с Яндекс Пэй">25 499 ₽</span>
      <span data-auto="snippet-price-old">27 499 ₽</span>
      <span data-zone-name="shopInShop">Проверенный магазин</span>
      <button data-auto="cartButton">В корзину</button>
    </article>
    """


def yandex_patch(payload):
    return f'<noframes data-apiary="patch">{json.dumps(payload, ensure_ascii=False)}</noframes>'


async def test_yandex_http_pay_offer_and_card_buyer_reviews(transport):
    search_html = yandex_tile() + yandex_patch({"product": {
        "id": 101000, "name": MODEL, "price": 25499, "greenPrice": 25499,
        "rating": 4.8, "opinions": 62, "image": "//avatars.mds.yandex.net/image.png",
    }})
    card_html = ld({**schema_product(price=25499), "additionalProperty": [
        {"name": "Разрешение", "value": "1920×1080"},
    ]}) + yandex_tile() + """
      <section data-auto="review-item"><div data-auto="review-comment">Хорошая картинка</div>
        <span data-auto="review-rating">5</span><time datetime="2026-11-20">20 ноября</time>
      </section>
    """ + yandex_patch({"reviews": [{"pros": "Тихий", "cons": "Тяжёлый", "comment": "Подходит для дома",
                                      "grade": 4, "created": "2026-11-23"}]})
    returns(transport.http_get, httpx.Response(200, text=search_html), httpx.Response(200, text=card_html))
    connector = YandexMarketConnector(transport)
    result = await connector.search(MODEL, model=MODEL)
    offer = result.offers[0]
    assert offer.url == "https://market.yandex.ru/card/proektor-wanbo/101000"
    assert offer.price == 27499 and offer.conditional_price == 25499
    assert offer.price_scope == "public" and offer.price_conditions == ["yandex_pay"]
    assert offer.rating == 4.8 and offer.reviews_count == 62
    assert offer.seller == "Проверенный магазин" and not result.outcome["fallback_used"]
    card = await connector.get_card("101000", model=MODEL)
    assert card.error is None and card.products[0].specifications["Разрешение"] == "1920×1080"
    assert card.offers[0].price == 27499 and card.offers[0].conditional_price == 25499
    assert {review["text"] for review in card.reviews} == {"Хорошая картинка", "Подходит для дома"}
    assert card.reviews[0]["pros"] == "Тихий"
    assert transport.http_get.call_args.kwargs["region"] == "RU"


async def test_yandex_captcha_http_falls_back_to_cdp(transport):
    returns(transport.http_get, httpx.Response(200, text="<title>Подтвердите, что вы не робот</title>"))
    returns(transport.cdp_fetch, browser(yandex_tile()))
    result = await YandexMarketConnector(transport).search(MODEL, model=MODEL)
    assert len(result.offers) == 1 and result.outcome["fallback_used"]
    assert any("captcha" in warning for warning in result.outcome["warnings"])


async def test_yandex_does_not_convert_a_pay_price_to_unconditional_through_schema(transport):
    url = "https://market.yandex.ru/card/proektor-wanbo/101000"
    html = ld(schema_product(price=25499, url=url)) + yandex_tile()
    returns(transport.http_get, httpx.Response(200, text=html))
    offer = (await YandexMarketConnector(transport).search(MODEL, model=MODEL)).offers[0]
    assert (offer.price, offer.conditional_price, offer.price_scope) == (27499, 25499, "public")


async def test_yandex_reviews_follow_the_matching_listing(transport):
    card = ld(schema_product()) + yandex_patch({"reviews": [
        {"comment": "Хорошая картинка", "grade": 5, "created": "2026-11-20"},
        {"comment": "Не подходит для дневного света", "grade": 3, "created": "2026-11-15"},
    ]})
    returns(transport.http_get, httpx.Response(200, text=yandex_tile()), httpx.Response(200, text=card))
    reviews, errors = await YandexMarketConnector(transport).get_reviews(MODEL, limit=1)
    assert errors == [] and len(reviews) == 1 and reviews[0]["marketplace"] == "yandex_market"
    assert reviews[0]["url"] == transport.http_get.call_args.args[0]


def avito_tile(sku=555666777, title=MODEL, condition="UsedCondition"):
    return f"""
    <div data-marker="item" itemscope itemtype="https://schema.org/Product">
      <a data-marker="item-title" href="/moskva/audio_i_video/proektor_{sku}"><h3 itemprop="name">{title}</h3></a>
      <meta itemprop="price" content="23000"><meta itemprop="priceCurrency" content="RUB">
      <meta itemprop="availability" content="https://schema.org/InStock">
      <link itemprop="itemCondition" href="https://schema.org/{condition}">
      <span data-marker="item-location">Москва</span>
    </div>
    """


def avito_item(sku=666777888, title=MODEL, **fields):
    return {
        "id": sku, "title": title, "priceDetailed": {"value": 21000, "currency": "RUB"},
        "urlPath": f"/omsk/audio_i_video/proektor_{sku}", "seller": {"name": "Алексей"},
        "location": {"name": "Омск"}, "images": [{"640x480": "https://static.avito.ru/product.jpg"}],
        **fields,
    }


async def test_avito_structured_and_microdata_skip_reserved_and_keep_condition(transport):
    returns(transport.cdp_fetch, browser(
        avito_tile() + avito_tile(999888777),
        payload={"result": {"items": [avito_item(), avito_item(999888777, isReserved=True),
                                       avito_item(444555666, status="archived")]}},
    ))
    result = await AvitoConnector(transport).search(MODEL, model=MODEL)
    unknown, used = result.offers
    assert (unknown.sku, unknown.price, unknown.condition, unknown.seller) == ("666777888", 21000, "unknown", "Алексей")
    assert (used.sku, used.price, used.condition) == ("555666777", 23000, "used")
    assert unknown.delivery == {"listing_location": "Омск"}
    assert used.delivery == {"listing_location": "Москва"}
    assert unknown.image_url == "https://static.avito.ru/product.jpg" and unknown.rating is None


async def test_avito_http_439_and_card_details(transport):
    returns(transport.cdp_fetch, browser(status=439), browser(
        ld(schema_product()) + """
        <main><h1>Wanbo Vali 1 Pro</h1>
          <meta itemprop="itemCondition" content="https://schema.org/UsedCondition">
          <ul data-marker="item-view/item-params"><li>Цвет: Белый</li></ul>
          <span data-marker="seller-info/name">Алексей</span>
          <span data-marker="item-view/item-address">Омск</span>
          <span data-marker="item-view/delivery">Авито Доставка</span>
        </main>
        """,
    ))
    returns(transport.camoufox_fetch, browser(avito_tile()))
    connector = AvitoConnector(transport)
    result = await connector.search(MODEL, model=MODEL)
    assert result.outcome["fallback_used"] and result.outcome["status"] == "ok"
    assert len(result.offers) == 1 and transport.camoufox_fetch.call_args.kwargs["allow_search"] is True
    card = await connector.get_card("555666777", model=MODEL)
    assert card.error is None and card.offers[0].price == 27000 and card.offers[0].condition == "used"
    assert card.offers[0].seller == "Алексей" and card.offers[0].rating is None
    assert card.offers[0].delivery == {"listing_location": "Омск", "badge": "Авито Доставка"}
    assert card.products[0].specifications == {"Цвет": "Белый"}


def ali_state(title=MODEL):
    return {"data": {"products": [{
        "productId": "1005001122334455", "title": title, "finalPrice": {"value": "24190.50", "currency": "RUB"},
        "originalPrice": "28 190 ₽", "averageStar": "4.8", "orders": 70,
        "productUrl": "/item/1005001122334455.html?src=tracking", "image": "//ae01.alicdn.com/card.jpg",
    }]}}


def ali_tile(sku, title=MODEL, price="22 150 ₽", extra=""):
    return f"""
    <article><a href="/item/{sku}.html"><h2>{title}</h2>
      <img src="//ae01.alicdn.com/dom.jpg"><del>26 000 ₽</del><ins>{price}</ins>
      <span data-testid="product-rating">4,7</span>{extra}</a></article>
    """


async def test_aliexpress_state_dom_stock_and_ruble_only_prices(transport):
    payload = f'<script id="__AER_DATA__">{json.dumps(ali_state())}</script>'
    html = (payload + ali_tile("1005002233445566") + ali_tile("1005003344556677", extra="Распродано")
            + ali_tile("1005004455667788", price="$110"))
    returns(transport.cdp_fetch, browser(html))
    result = await AliExpressConnector(transport).search(MODEL, model=MODEL)
    structured, dom = result.offers
    assert structured.price == Decimal("24190.50") and structured.original_price == 28190
    assert structured.rating == 4.8 and structured.reviews_count is None
    assert "Orders=70" in structured.evidence.excerpts
    assert structured.url == "https://aliexpress.ru/item/1005001122334455.html"
    assert dom.price == 22150 and dom.original_price == 26000 and dom.rating == 4.7
    assert all(offer.availability == "in_stock" and offer.currency == "RUB" for offer in result.offers)
    assert any("no sold-out indicator" in excerpt for excerpt in dom.evidence.excerpts)


async def test_aliexpress_card_gets_explicit_stock_and_specs(transport):
    returns(transport.cdp_fetch, browser(ld(schema_product(price=24190)) + """
      <h1>Wanbo Vali 1 Pro</h1><div data-testid="product-details">
        <del>28 190 ₽</del><span data-testid="product-orders">70 заказов</span>
        <dl><dt>Разрешение</dt><dd>1920×1080</dd></dl>
      </div>
    """))
    card = await AliExpressConnector(transport).get_card("1005001122334455", model=MODEL)
    assert not card.error and card.offers[0].price == 24190 and card.offers[0].original_price == 28190
    assert card.offers[0].availability == "in_stock"
    assert card.products[0].specifications == {"Разрешение": "1920×1080"}


async def test_aliexpress_live_dom_tile_uses_sale_price_and_ignores_from_price(transport):
    title = "Сменные амбушюры для Sony WH-1000XM5 из пены с эффектом памяти"
    returns(transport.cdp_fetch, browser(f"""
      <script id="__AER_DATA__">{{"data":{{"products":[]}}}}</script>
      <div class="grid"><div class="tile">
        <div><a href="/item/1005001122334455.html?sku_id=120000420&src=tracking"><img src="/card.jpg"></a></div>
        <div><span>647\u00a0₽</span><span>-58%</span><span>272\u00a0₽</span></div>
        <span>5.0</span><span>14 купили</span><span>{title}</span><span>от 315\u00a0₽</span>
        <a href="https://aliexpress.ru/item/1005001122334455.html?src=duplicate"></a>
      </div><div><a href="/item/1005002233445566.html">Другие амбушюры</a><span>от 99 ₽</span></div></div>
    """))
    result = await AliExpressConnector(transport).search(title)
    offer, = result.offers
    assert offer.title == title
    assert offer.url == "https://aliexpress.ru/item/1005001122334455.html?sku_id=120000420"
    assert (offer.price, offer.original_price, offer.rating, offer.availability) == (272, 647, 5.0, "in_stock")
    assert "Orders=14" in offer.evidence.excerpts and offer.reviews_count is None


@pytest.mark.parametrize("connector_type, method, response", [
    (WildberriesConnector, "http_get", httpx.Response(200, json={"products": [wb_item(title=WRONG_MODEL)]})),
    (OzonConnector, "cdp_fetch", browser(payload=ozon_widget(WRONG_MODEL))),
    (YandexMarketConnector, "http_get", httpx.Response(200, text=yandex_tile(WRONG_MODEL))),
    (AvitoConnector, "cdp_fetch", browser(payload={"items": [avito_item(title=WRONG_MODEL)]})),
    (AliExpressConnector, "cdp_fetch", browser(payload=ali_state(WRONG_MODEL))),
])
async def test_search_rejects_wrong_exact_model_across_marketplaces(transport, connector_type, method, response):
    returns(getattr(transport, method), response)
    result = await connector_type(transport).search("Wanbo", model=MODEL)
    assert result.offers == result.products == []
    assert result.outcome["status"] == "ok" and result.outcome["offers_returned"] == 0


@pytest.mark.parametrize("failure, expected", [
    ({"error": "http_403", "http_status": 403}, "blocked"),
    (AccessError("browser_timeout"), "timeout"),
    ({"error": "worker_unavailable"}, "error"),
])
async def test_outcomes_distinguish_access_failures(transport, failure, expected):
    returns(transport.cdp_fetch, failure)
    result = await AliExpressConnector(transport).search(MODEL)
    assert result.outcome["status"] == expected and result.outcome["offers_returned"] == 0
    assert result.outcome["error_code"] == result.errors[0]["error"]
    assert result.outcome["elapsed_ms"] >= 0


@pytest.mark.parametrize("connector_type", [
    WildberriesConnector, OzonConnector, YandexMarketConnector, AvitoConnector, AliExpressConnector,
])
async def test_card_rejects_foreign_host(transport, connector_type):
    result = await connector_type(transport).get_card("https://outside.example/product/projector-123456/")
    assert result.error == "invalid_card_url_or_sku"
