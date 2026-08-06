import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from rss2discord.transports import FeedFetchError, gjirafa50_parser
from rss2discord.transports.gjirafa50_parser import parse_gjirafa50_page
from tests.gjirafa50_helpers import catalog_payload


def test_parser_accepts_fractional_mkd_price_from_live_catalog() -> None:
    payload = catalog_payload(1, [(1, Decimal("12968.50"))])

    page = parse_gjirafa50_page(payload, datetime.now(UTC))

    assert page.products[0].price == Decimal("12968.50")
    assert page.products[0].formatted_price == "12.968,50 MKD."


def test_parser_rejects_price_below_mkd_cent_precision() -> None:
    payload = catalog_payload(1, [(1, Decimal("1.50"))]).replace(b"1,50", b"1,505")

    with pytest.raises(FeedFetchError, match="InvalidProduct"):
        parse_gjirafa50_page(payload, datetime.now(UTC))


def test_parser_rejects_product_url_with_credentials_or_explicit_port() -> None:
    payload = catalog_payload(1, [(1, 100)]).replace(
        b"/product-1",
        b"https://user:pass@gjirafa50.mk:444/product-1",
    )

    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        parse_gjirafa50_page(payload, datetime.now(UTC))


def test_parser_rejects_image_url_with_credentials_or_fragment() -> None:
    payload = catalog_payload(1, [(1, 100)]).replace(
        b"https://50cdn.gjirafamall.tech/1.jpg",
        b"https://user:pass@50cdn.gjirafamall.tech/1.jpg#fragment",
    )

    with pytest.raises(FeedFetchError, match="InvalidImageUrl"):
        parse_gjirafa50_page(payload, datetime.now(UTC))


def test_parser_derives_display_price_from_validated_numeric_price() -> None:
    payload = catalog_payload(1, [(1, 1_234)]).replace(
        b"1234,00 MKD.",
        b"[click](https://attacker.example)",
    )

    page = parse_gjirafa50_page(payload, datetime.now(UTC))

    assert page.products[0].formatted_price == "1.234,00 MKD."


def test_parser_rejects_declared_product_count_above_page_size() -> None:
    payload = catalog_payload(25, [(product_id, 100) for product_id in range(1, 26)])

    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        parse_gjirafa50_page(payload, datetime.now(UTC))


def test_parser_rejects_nested_card_excess_before_parsing_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "totalpages": 1,
            "totalHits": 24,
            "productsCount": 24,
            "html": '<div class="product-item">' * 25 + "</div>" * 25,
        },
    ).encode()
    parse_product = Mock(side_effect=AssertionError("product parsing must not start"))
    monkeypatch.setattr(gjirafa50_parser, "_parse_product", parse_product)

    with pytest.raises(FeedFetchError, match="InvalidCardinality"):
        parse_gjirafa50_page(payload, datetime.now(UTC))

    parse_product.assert_not_called()
