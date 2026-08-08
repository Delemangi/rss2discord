import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest

from rss2discord.transports import FeedFetchError, gjirafa50_parser
from rss2discord.transports.gjirafa50_parser import parse_gjirafa50_page
from tests.gjirafa50_helpers import ROOT_URL, catalog_payload


def test_parser_accepts_fractional_mkd_price_from_live_catalog() -> None:
    payload = catalog_payload(1, [(1, Decimal("12968.50"))])

    page = parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)

    assert page.products[0].price == Decimal("12968.50")
    assert page.products[0].formatted_price == "12.968,50 MKD."


def test_parser_accepts_eur_price_from_live_com_catalog() -> None:
    payload = catalog_payload(1, [(1, Decimal("969.50"))]).replace(
        b' data-discountedprice=\\"969,50\\"',
        b' data-discountedprice=\\"969,5000\\"',
    )

    page = parse_gjirafa50_page(
        payload,
        datetime.now(UTC),
        "https://gjirafa50.com/",
    )

    assert page.products[0].price == Decimal("969.5000")
    assert page.products[0].currency == "EUR"
    assert page.products[0].formatted_price == "969.50 €"


def test_parser_accepts_dot_grouped_mkd_price() -> None:
    payload = catalog_payload(1, [(1, Decimal("12968.50"))]).replace(
        b' data-discountedprice=\\"12968,50\\"',
        b' data-discountedprice=\\"12.968,50\\"',
    )

    page = parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)

    assert page.products[0].price == Decimal("12968.50")


def test_parser_rejects_price_below_mkd_cent_precision() -> None:
    payload = catalog_payload(1, [(1, Decimal("1.50"))]).replace(b"1,50", b"1,505")

    with pytest.raises(FeedFetchError, match="InvalidProduct"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)


@pytest.mark.parametrize("price", [b"12.34", b"1.2,50", b"1,2"])
def test_parser_rejects_price_outside_provider_grammar(price: bytes) -> None:
    payload = catalog_payload(1, [(1, Decimal("100.00"))]).replace(
        b' data-discountedprice=\\"100,00\\"',
        b' data-discountedprice=\\"' + price + b'\\"',
    )

    with pytest.raises(FeedFetchError, match="InvalidProduct"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)


def test_parser_rejects_oversized_price_before_decimal_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_price = b"1" * 1_000 + b",00"
    payload = catalog_payload(1, [(1, Decimal("100.00"))]).replace(
        b' data-discountedprice=\\"100,00\\"',
        b' data-discountedprice=\\"' + oversized_price + b'\\"',
    )
    parse_decimal = Mock(
        side_effect=AssertionError("Decimal conversion must not start"),
    )
    monkeypatch.setattr(gjirafa50_parser, "Decimal", parse_decimal)

    with pytest.raises(FeedFetchError, match="InvalidProduct"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)

    parse_decimal.assert_not_called()


def test_parser_rejects_product_url_with_credentials_or_explicit_port() -> None:
    payload = catalog_payload(1, [(1, 100)]).replace(
        b"/product-1",
        b"https://user:pass@gjirafa50.mk:444/product-1",
    )

    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)


def test_parser_rejects_product_url_from_other_storefront() -> None:
    payload = catalog_payload(1, [(1, 100)]).replace(
        b"/product-1",
        b"https://gjirafa50.mk/product-1",
    )

    with pytest.raises(FeedFetchError, match="InvalidProductUrl"):
        parse_gjirafa50_page(
            payload,
            datetime.now(UTC),
            "https://gjirafa50.com/",
        )


def test_parser_rejects_image_url_with_credentials_or_fragment() -> None:
    payload = catalog_payload(1, [(1, 100)]).replace(
        b"https://50cdn.gjirafamall.tech/1.jpg",
        b"https://user:pass@50cdn.gjirafamall.tech/1.jpg#fragment",
    )

    with pytest.raises(FeedFetchError, match="InvalidImageUrl"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)


def test_parser_derives_display_price_from_validated_numeric_price() -> None:
    payload = catalog_payload(1, [(1, 1_234)]).replace(
        b"1234,00 MKD.",
        b"[click](https://attacker.example)",
    )

    page = parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)

    assert page.products[0].formatted_price == "1.234,00 MKD."


def test_parser_rejects_declared_product_count_above_page_size() -> None:
    payload = catalog_payload(25, [(product_id, 100) for product_id in range(1, 26)])

    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)


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
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)

    parse_product.assert_not_called()


def test_parser_rejects_html_over_tag_budget_before_building_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "totalpages": 0,
            "totalHits": 0,
            "productsCount": 0,
            "html": "<div></div><div></div>",
        },
    ).encode()
    parse_html = Mock(side_effect=AssertionError("HTML parsing must not start"))
    monkeypatch.setattr(gjirafa50_parser, "MAX_GJIRAFA50_HTML_TAG_TOKENS", 3)
    monkeypatch.setattr(gjirafa50_parser, "BeautifulSoup", parse_html)

    with pytest.raises(FeedFetchError, match="HtmlComplexityExceeded"):
        parse_gjirafa50_page(payload, datetime.now(UTC), ROOT_URL)

    parse_html.assert_not_called()
