"""Parsing tests for the Wolfinet Odoo shop page."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.providers.wolfinet.parser import (
    format_wolfinet_price,
    parse_wolfinet_page,
    parse_wolfinet_price,
)

FIXTURES = Path(__file__).parent / "fixtures" / "wolfinet"
OBSERVED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_wolfinet_parser_extracts_valid_cards_in_page_order() -> None:
    products = parse_wolfinet_page(
        read_fixture("shop-page.html"),
        observed_at=OBSERVED_AT,
    )

    assert [product.product_id for product in products] == ["4101", "4102", "4103"]
    assert [product.name for product in products] == [
        "Ubiquiti UniFi 6 Lite",
        "TP-Link Omada EAP225",
        "MikroTik CRS328-24P-4S+RM",
    ]
    assert [product.url for product in products] == [
        "https://wolfinet.com.mk/en/shop/ubiquiti-unifi-6-lite-4101",
        "https://wolfinet.com.mk/en/shop/tp-link-omada-eap225-4102",
        "https://wolfinet.com.mk/en/shop/mikrotik-crs328-24p-4s-rm-4103",
    ]
    assert [product.image_url for product in products] == [
        "https://wolfinet.com.mk/web/image/product.template/4101/image_1920",
        "https://wolfinet.com.mk/web/image/product.template/4102/image_1920",
        "https://wolfinet.com.mk/web/image/product.template/4103/image_1920",
    ]
    assert products[0].description == "Wi-Fi 6 access point & PoE powered."
    assert products[2].description == "24-port PoE switch with four SFP+ ports."
    assert [product.observed_at for product in products] == [OBSERVED_AT] * 3
    assert [product.currency for product in products] == ["MKD"] * 3


def test_wolfinet_parser_strips_product_link_query() -> None:
    products = parse_wolfinet_page(read_fixture("shop-page.html"))

    assert all("?" not in product.url for product in products)


def test_wolfinet_parser_parses_listing_prices_and_formats_them() -> None:
    products = parse_wolfinet_page(read_fixture("shop-page.html"))

    assert [product.amount for product in products] == [
        Decimal("2364.41"),
        Decimal("9500.00"),
        Decimal("16550.00"),
    ]
    assert [product.formatted_price for product in products] == [
        "2,364.41 ден",
        "9,500.00 ден",
        "16,550.00 ден",
    ]


def test_wolfinet_parser_skips_malformed_external_and_unpriced_cards() -> None:
    products = parse_wolfinet_page(read_fixture("shop-page.html"))

    assert len(products) == 3
    names = {product.name for product in products}
    assert "Broken card" not in names
    assert "External card" not in names
    assert "Unpriced item" not in names


def test_wolfinet_parser_returns_empty_products_for_an_empty_page() -> None:
    assert parse_wolfinet_page(read_fixture("empty-page.html")) == ()


def test_wolfinet_product_id_comes_from_the_final_path_segment() -> None:
    products = parse_wolfinet_page(
        (
            '<div class="o_wsale_product_grid_wrapper">'
            '<div class="product_price">'
            '<span class="oe_currency_value">100.00</span> ден'
            "</div>"
            '<h6><a itemprop="name" '
            'href="/en/shop/cat-6-patch-cable-1595?order=create_date+desc">'
            "Patch Cable</a></h6>"
            '<img itemprop="image" src="/web/image/product.template/1595/image_1920"/>'
            "</div>"
        ),
        observed_at=OBSERVED_AT,
    )

    assert products[0].product_id == "1595"
    assert products[0].url == ("https://wolfinet.com.mk/en/shop/cat-6-patch-cable-1595")
    assert products[0].image_url == (
        "https://wolfinet.com.mk/web/image/product.template/1595/image_1920"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2,364.41", Decimal("2364.41")),
        ("2,364.41 ден", Decimal("2364.41")),
        ("9,500.00 ден", Decimal("9500.00")),
        ("1,234", Decimal(1234)),
        ("100 MKD", Decimal(100)),
    ],
)
def test_wolfinet_parse_price_accepts_supported_formats(
    value: str,
    expected: Decimal,
) -> None:
    assert parse_wolfinet_price(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "цена", "0.00", "10,31.00", "10.315.00"],
)
def test_wolfinet_parse_price_rejects_invalid_or_non_positive_values(
    value: str,
) -> None:
    assert parse_wolfinet_price(value) is None


def test_wolfinet_format_price_matches_the_listing_style() -> None:
    assert format_wolfinet_price(Decimal("2364.41")) == "2,364.41 ден"
    assert format_wolfinet_price(Decimal(16550)) == "16,550.00 ден"
