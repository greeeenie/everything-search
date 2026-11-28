from decimal import Decimal

import pytest

from conftest import product_page
from research_v2.shopping import adapter_for, identity, model_matches, product_matches, review_excerpts


@pytest.mark.parametrize("host", ["ozon.ru", "avito.ru", "market.yandex.ru"])
def test_shared_adapter_contract(host):
    page = product_page(host=host)
    products, offers = adapter_for(page.url).parse(page, "Wanbo Cube 2 Pro")
    assert len(products) == len(offers) == 1
    assert offers[0].price == Decimal("12990")
    assert offers[0].currency == "RUB" and offers[0].condition == "new"
    assert offers[0].availability == "in_stock"
    assert offers[0].url == page.url


def test_exact_model_normalization():
    assert identity("Wanbo Cube 2 Pro") == identity("WANBO Cube ２ Pro")
    assert identity("Wanbo Cube2 Pro") == identity("Wanbo Cube 2 Pro")
    assert identity("Wanbo Cube 2 Pro") != identity("Wanbo Cube 1")
    assert not model_matches("Wanbo Cube 2 Pro", "Wanbo X5 Pro")
    assert not model_matches("Wanbo Cube 2", "Wanbo Cube 2 Pro")
    assert not model_matches("Wanbo Cube 1", "Wanbo Cube 11")
    assert not product_matches("Raspberry Pi 5", "Raspberry Pi 5 Starter Kit")
    assert not product_matches("Raspberry Pi 5", "Case for Raspberry Pi 5")
    assert product_matches("Raspberry Pi 5", "Raspberry Pi 5 - 8 GB RAM")
    assert product_matches("Raspberry Pi 5 Starter Kit", "Raspberry Pi 5 Starter Kit")


def test_product_matching_rejects_spare_parts_and_compatibility_titles():
    assert not product_matches("Sony WH-1000XM5", "Запасная дуга для Sony WH-1000XM5, black")
    assert not product_matches(
        "Sony WH-1000XM5", "Сменные амбушюры для Sony WH-1000XM5 из пены с эффектом памяти",
    )
    assert not product_matches("Sony WH-1000XM5", "Шарнир Sony WH-1000XM5")
    assert not product_matches("Sony WH-1000XM5", "Комплектующие для наушников Sony WH-1000XM5")
    assert not product_matches("iPhone 16", "Case for iPhone 16")
    assert product_matches("Sony WH-1000XM5", "Беспроводные/проводные наушники Sony WH-1000XM5 черный 2023")
    assert not product_matches("амбушюры для Sony WH-1000XM5", "Сменные амбушюры для Sony WH-1000XM5")
    assert product_matches("амбушюры для Sony WH-1000XM5", "Амбушюры для Sony WH-1000XM5")


def test_wrong_model_has_no_offer():
    page = product_page(name="Wanbo X5 Pro")
    assert adapter_for(page.url).parse(page, "Wanbo Cube 2 Pro") == ([], [])


def test_model_matching_uses_explicit_brand_field():
    page = product_page(name="Cube 2 Pro")
    products, offers = adapter_for(page.url).parse(page, "Wanbo Cube 2 Pro")
    assert len(products) == len(offers) == 1


def test_conditional_card_price():
    page = product_page(extra_text="Цена по карте Ozon")
    _, offers = adapter_for(page.url).parse(page)
    assert offers[0].price_scope == "conditional"
    assert "bank_card" in offers[0].price_conditions


def test_aggregate_price_is_not_seller_offer():
    page = product_page()
    page.structured_data[0]["offers"] = {"@type": "AggregateOffer", "lowPrice": "100", "priceCurrency": "RUB"}
    products, offers = adapter_for(page.url).parse(page)
    assert products and offers == []


def test_indexed_price_is_not_used():
    page = product_page()
    page.status = "indexed_only"
    assert adapter_for(page.url).parse(page) == ([], [])


def test_unknown_condition_and_currency_are_not_guessed():
    page = product_page(currency="", condition="")
    _, offers = adapter_for(page.url).parse(page)
    assert offers[0].price is None and offers[0].currency is None and offers[0].condition == "unknown"


def test_reviews_bound_to_model():
    page = product_page()
    page.structured_data[0]["review"] = {"reviewBody": "Specific owner report", "datePublished": "2026-11-01"}
    assert review_excerpts(page, "Wanbo Cube 2 Pro")[0]["model"] == "Wanbo Cube 2 Pro"
    assert review_excerpts(page, "Wanbo X5 Pro") == []


def test_bracketed_specs_do_not_trigger_accessory_filter():
    dns_title = "Беспроводные/проводные наушники Sony WH-1000XM5 черный 2023 [охватывающие, проводной, Bluetooth 5.2, кабель - 1.5 м]"
    assert product_matches("Sony WH-1000XM5", dns_title)
    assert not product_matches("Sony WH-1000XM5", "Кабель зарядки для Sony WH-1000XM5")
