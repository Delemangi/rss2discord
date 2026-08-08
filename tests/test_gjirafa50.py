from datetime import UTC, datetime
from decimal import Decimal

from rss2discord.models import SourceMetric
from rss2discord.transports.gjirafa50 import Gjirafa50Strategy, format_gjirafa50_mkd
from rss2discord.transports.gjirafa50_models import Gjirafa50Product


def test_gjirafa50_strategy_maps_product_to_entry() -> None:
    # Given
    product = Gjirafa50Product(
        id=42,
        title="Laptop",
        link="https://gjirafa50.mk/laptop",
        image_url="https://50cdn.gjirafamall.tech/laptop.jpg",
        price=Decimal("13990.00"),
        currency="MKD",
        formatted_price="13.990 MKD.",
        observed_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
    )

    # When
    entry = Gjirafa50Strategy().get_entry_data(product)

    # Then
    assert Gjirafa50Strategy().get_entry_id(product) == "42"
    assert entry.title == "Laptop"
    assert entry.link == "https://gjirafa50.mk/laptop"
    assert entry.timestamp == "2026-08-05T12:00:00+00:00"
    assert entry.source_metrics == (SourceMetric("Price", "13.990 MKD."),)


def test_gjirafa50_mkd_format_matches_storefront() -> None:
    assert format_gjirafa50_mkd(Decimal("1234567.50")) == "1.234.567,5 MKD."
