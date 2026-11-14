from research_v2.fetching import extract
from research_v2.models import Page
from research_v2.shopping import adapter_for
from research_v2.engine import Engine
from test_engine import FakeDiscovery, FakeFetcher

HTML = """<html><h1>Поиск</h1><noframes data-apiary="patch">{"currentUser":"private transport state"}</noframes>
<div data-auto="searchOrganic">
 <a href="/card/wanbo-cube-2-pro/123456?do-waremd5=offer-1&amp;cpc=tracking"><span data-auto="snippet-title">Проектор Wanbo Projector Cube 2 Pro, белый</span></a>
 <a aria-label="Цена с картой Яндекс Пэй"><span data-auto="snippet-price-current">12 345 ₽</span>Цена с картой Яндекс Пэй</a>
 <div data-zone-name="shopInShop">Продавец Один</div><button data-auto="cartButton" aria-label="В корзину">В корзину</button>
</div>
<div data-auto="searchOrganic">
 <a data-auto="snippet-title" href="/card/wanbo-cube-1/987654">Проектор Wanbo Cube 1</a>
 <span data-auto="snippet-price-current">5 000 ₽</span>
</div></html>"""


def page():
    url = "https://market.yandex.ru/search?text=Wanbo+Cube+2+Pro"
    title, text, nodes = extract(HTML, url)
    return Page(
        url=url,
        source="public_page",
        status="verified",
        region="RU",
        title=title,
        text=text,
        structured_data=nodes,
    )


def test_market_search_dom_produces_verified_conditional_offer():
    p = page()
    products, offers = adapter_for(p.url).parse(p, "Wanbo Cube 2 Pro")
    assert len(products) == len(offers) == 1
    offer = offers[0]
    assert str(offer.price) == "12345" and offer.currency == "RUB"
    assert offer.seller == "Продавец Один" and offer.availability == "in_stock"
    assert offer.price_scope == "conditional" and offer.condition == "unknown"
    assert "cpc=" not in offer.url and "do-waremd5=offer-1" in offer.url
    assert offer.evidence.url == p.url and offer.evidence.status == "verified"


def test_transport_state_is_excluded_from_page_text():
    assert "private transport state" not in page().text


async def test_native_market_search_works_without_index_results(settings, store):
    p = page()
    settings.native_search_marketplaces = ["yandex_market"]
    engine = Engine(settings, store, FakeFetcher([p]), FakeDiscovery([]))
    result = await engine.search_products(
        "Wanbo Cube 2 Pro", model="Wanbo Cube 2 Pro", marketplaces=["yandex_market"]
    )
    assert len(result["offers"]) == 1 and result["candidates"][0]["status"] == "verified"
