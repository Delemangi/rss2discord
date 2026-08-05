from datetime import UTC, datetime
from decimal import Decimal

import pytest

from rss2discord.transports import FeedFetchError
from rss2discord.transports.gjirafa50_parser import parse_gjirafa50_page
from tests.gjirafa50_helpers import catalog_payload


def test_parser_rejects_fractional_price_outside_integer_partition_domain() -> None:
    payload = catalog_payload(1, [(1, Decimal("1.50"))])

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
