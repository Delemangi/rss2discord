"""Parsing tests for the PROXNet category page."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.providers.proxnet.parser import (
    parse_mkd_price,
    parse_proxnet_page,
)

FIXTURES = Path(__file__).parent / "fixtures" / "proxnet"
OBSERVED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_proxnet_parser_extracts_valid_cards_in_page_order() -> None:
    page = parse_proxnet_page(
        read_fixture("category-page.html"),
        observed_at=OBSERVED_AT,
    )

    assert page.title == "Мрежна опрема"
    assert [product.product_id for product in page.products] == [
        "1152",
        "2048",
        "3310",
    ]
    assert [product.name for product in page.products] == [
        "Heat Shrink Sleeves 40 mm",
        "Cat6 Patch Cable 2 m",
        "Network Switch 8-Port",
    ]
    assert [product.url for product in page.products] == [
        "https://www.proxnet.mk/mk/1152-heat-shrink-sleeves-40-mm.html",
        "https://www.proxnet.mk/mk/2048-cat6-patch-cable-2-m.html",
        "https://www.proxnet.mk/mk/3310-network-switch-8-port.html",
    ]
    assert [product.image_url for product in page.products] == [
        "https://www.proxnet.mk/1152-large/heat-shrink.jpg",
        "https://www.proxnet.mk/2048-large/cat6-cable.jpg",
        "https://www.proxnet.mk/3310-large/switch.jpg",
    ]
    assert page.products[0].description == ("Термо смалувачки ракав 40 mm & заштита")
    assert [product.category for product in page.products] == ["Мрежна опрема"] * 3
    assert [product.observed_at for product in page.products] == [OBSERVED_AT] * 3
    assert [product.currency for product in page.products] == ["MKD"] * 3


def test_proxnet_parser_parses_supported_price_formats() -> None:
    page = parse_proxnet_page(read_fixture("category-page.html"))

    assert [product.amount for product in page.products] == [
        Decimal("9.00"),
        Decimal("10315.00"),
        Decimal("8950.01"),
    ]
    assert [product.formatted_price for product in page.products] == [
        "9.00 ден",
        "10,315.00 ден",
        "8,950.01 ден",
    ]


def test_proxnet_parser_skips_malformed_external_and_unpriced_cards() -> None:
    page = parse_proxnet_page(read_fixture("category-page.html"))

    assert len(page.products) == 3
    assert "External card" not in {product.name for product in page.products}
    assert "Unpriced item" not in {product.name for product in page.products}


def test_proxnet_parser_returns_empty_products_for_an_empty_page() -> None:
    page = parse_proxnet_page(read_fixture("empty-page.html"))

    assert page.products == ()
    assert page.title == "Мрежна опрема"


def test_proxnet_page_title_uses_the_category_name_without_the_counter() -> None:
    page = parse_proxnet_page(
        (
            '<h1 class="page-heading product-listing">'
            '<span class="cat-name">Мрежна опрема&nbsp;</span>'
            '<span class="heading-counter">Има 651 продукти во оваа категорија</span>'
            "</h1>"
        ),
        observed_at=OBSERVED_AT,
    )

    assert page.title == "Мрежна опрема"


def test_proxnet_product_id_comes_from_the_last_path_segment() -> None:
    page = parse_proxnet_page(
        (
            '<li class="ajax_block_product">'
            '<a class="product-name" '
            'href="https://www.proxnet.mk/mk/cat-6/1595-patch-cable.html">'
            "Patch Cable</a>"
            '<span class="price product-price">100.00 ден</span>'
            "</li>"
        ),
        observed_at=OBSERVED_AT,
    )

    assert page.products[0].product_id == "1595"
    assert page.products[0].url == (
        "https://www.proxnet.mk/mk/cat-6/1595-patch-cable.html"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("9.00 ден", Decimal("9.00")),
        ("10,315.00 ден", Decimal("10315.00")),
        ("8,950.01 ден", Decimal("8950.01")),
        ("1,234 ден", Decimal(1234)),
        ("100 MKD", Decimal(100)),
    ],
)
def test_proxnet_parse_mkd_price_accepts_supported_formats(
    value: str,
    expected: Decimal,
) -> None:
    assert parse_mkd_price(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "цена", "0.00 ден", "10,31.00 ден", "10.315.00 ден"],
)
def test_proxnet_parse_mkd_price_rejects_invalid_or_non_positive_values(
    value: str,
) -> None:
    assert parse_mkd_price(value) is None
