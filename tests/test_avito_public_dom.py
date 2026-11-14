from research_v2.fetching import extract
from research_v2.models import Page
from research_v2.shopping import adapter_for


def test_avito_microdata_keeps_item_price_and_state_separate():
    html = """<html><div itemscope itemtype="https://schema.org/Product" data-marker="item">
    <h2 itemprop="name"><a data-marker="item-title" href="/omsk/audio_i_video/wanbo_cube_2_pro_12345678?context=tracking">Проектор Wanbo Cube 2 Pro</a></h2>
    <p itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <meta itemprop="price" content="12500"><meta itemprop="priceCurrency" content="RUB">
    <meta itemprop="availability" content="https://schema.org/LimitedAvailability">
    <meta itemprop="itemCondition" content="https://schema.org/UsedCondition"></p>
    <p>Почти новый, включали для обзора</p></div>
    <div itemscope itemtype="https://schema.org/Product"><h2 itemprop="name">Wanbo Cube 1</h2>
    <a itemprop="url" href="/omsk/audio_i_video/cube_1_98765432"></a>
    <meta itemprop="price" content="1000"><meta itemprop="priceCurrency" content="RUB"></div></html>"""
    url = "https://www.avito.ru/all?q=Wanbo+Cube+2+Pro"
    title, text, nodes = extract(html, url)
    page = Page(
        url=url,
        source="public_page",
        status="verified",
        region="RU",
        title=title,
        text=text,
        structured_data=nodes,
    )
    products, offers = adapter_for(url).parse(page, "Wanbo Cube 2 Pro")
    assert len(products) == len(offers) == 1
    offer = offers[0]
    assert offer.price == 12500 and offer.condition == "used"
    assert offer.price_scope == "public" and offer.availability == "in_stock"
    assert "context=" not in offer.url and offer.evidence.url == url


def test_avito_marketing_description_does_not_imply_new_condition():
    url = "https://www.avito.ru/all?q=model"
    _, text, nodes = extract(
        """<div itemscope itemtype="https://schema.org/Product">
    <h2 itemprop="name">Wanbo Cube 2 Pro</h2><a itemprop="url" href="/omsk/a/model_12345678"></a>
    <meta itemprop="price" content="12000"><meta itemprop="priceCurrency" content="RUB">
    <p>Почти новый, считай новый</p></div>""",
        url,
    )
    page = Page(
        url=url, source="public_page", status="verified", region="RU", text=text, structured_data=nodes
    )
    _, offers = adapter_for(url).parse(page, "Wanbo Cube 2 Pro")
    assert offers[0].condition == "unknown" and offers[0].availability == "unknown"


async def test_condition_filter_reads_individual_ad(settings, store):
    from conftest import product_page
    from research_v2.engine import Engine
    from test_engine import FakeDiscovery, FakeFetcher

    detail = product_page(host="avito.ru", condition="UsedCondition", price="9000")
    native_url = adapter_for(detail.url).search_url("Wanbo Cube 2 Pro")
    search = detail.model_copy(deep=True)
    search.url = search.final_url = native_url
    search.structured_data[0]["offers"]["url"] = detail.url
    search.structured_data[0]["offers"].pop("itemCondition")
    settings.native_search_marketplaces = ["avito"]
    engine = Engine(settings, store, FakeFetcher([search, detail]), FakeDiscovery([]))
    result = await engine.search_products(
        "Wanbo Cube 2 Pro", model="Wanbo Cube 2 Pro", marketplaces=["avito"], condition="used"
    )
    assert result["condition_details_checked"] == 1
    assert len(result["offers"]) == 1 and result["offers"][0]["condition"] == "used"
    assert result["offers"][0]["evidence"]["url"] == detail.url
