from typing import Literal, assert_never

import pytest
from curl_cffi import requests

from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from tests.neksio_helpers import (
    CATALOG_URL,
    RecordingGet,
    RecordingPost,
    StubResponse,
    homepage_payload,
    page_payload,
    product_card,
)

type DisplayField = Literal[
    "productName",
    "productCode",
    "category",
    "subCategory",
    "manufacturer",
    "priceWTax_f",
    "old_PriceWTax",
]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("productName", "Product\nforged line"),
        ("productCode", "CODE\rforged"),
        ("category", "Laptops\x00forged"),
        ("subCategory", "Gaming\x7fforged"),
        ("manufacturer", "Vendor\tforged"),
        ("priceWTax_f", "1.200 MKD\nforged"),
        ("old_PriceWTax", "1.500 MKD\rforged"),
    ],
)
def test_fetch_catalog_normalizes_control_characters_in_display_text(
    monkeypatch: pytest.MonkeyPatch,
    field: DisplayField,
    value: str,
) -> None:
    # Given
    product = product_card(1)
    match field:
        case "productName":
            product["productName"] = value
        case "productCode":
            product["productCode"] = value
        case "category":
            product["category"] = value
        case "subCategory":
            product["subCategory"] = value
        case "manufacturer":
            product["manufacturer"] = value
        case "priceWTax_f":
            product["priceWTax_f"] = value
        case "old_PriceWTax":
            product["old_PriceWTax"] = value
        case unreachable:
            assert_never(unreachable)
    monkeypatch.setattr(
        requests,
        "get",
        RecordingGet([StubResponse(homepage_payload([1]))]),
    )
    monkeypatch.setattr(
        requests,
        "post",
        RecordingPost([StubResponse(page_payload(1, 1, 1, 1, [product]))]),
    )

    # When
    observed = NeksioCatalogClient().fetch_catalog(CATALOG_URL)[0]

    # Then
    display_values = (
        observed.product_name,
        observed.product_code,
        observed.category,
        observed.subcategory,
        observed.manufacturer,
        observed.formatted_price,
        observed.old_formatted_price or "",
    )
    assert all(
        all(ord(character) >= 32 and ord(character) != 127 for character in text)
        for text in display_values
    )
