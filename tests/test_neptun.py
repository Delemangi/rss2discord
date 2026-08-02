from decimal import Decimal

import pytest
from pydantic import ValidationError

from rss2discord.models import SourceMetric
from rss2discord.transports.neptun import NeptunStrategy, format_neptun_mkd
from rss2discord.transports.neptun_models import NeptunProduct
from tests.neptun_helpers import product_payload


def make_product(
    *,
    actual_price: Decimal | int = 1_999,
    date_inserted: str = "2026-07-21T15:44:34.97",
) -> NeptunProduct:
    return NeptunProduct.model_validate(
        product_payload(
            42,
            actual_price=actual_price,
            date_inserted=date_inserted,
        ),
    )


def test_neptun_strategy_has_safe_seed_and_history_contract() -> None:
    strategy = NeptunStrategy()

    assert strategy.seed_existing_on_first_fetch
    assert strategy.require_entries_for_initialization
    assert strategy.max_new_entries_per_fetch == 30
    assert strategy.max_delivery_history == 10_000


def test_neptun_strategy_maps_product_to_rich_entry() -> None:
    strategy = NeptunStrategy()
    product = make_product()

    entry_id = strategy.get_entry_id(product)
    entry = strategy.get_entry_data(product)

    assert entry_id == "42"
    assert entry.title == "Product 42"
    assert entry.link == "https://www.neptun.mk/categories/KOMPJUTERI/product-42"
    assert entry.image_url == "https://www.neptun.mk/Content/Images/42.jpg"
    assert entry.timestamp == "2026-07-21T15:44:34.970000+02:00"
    assert entry.categories == ("Computers",)
    assert entry.source_metrics == (
        SourceMetric("Price", "1.999 ден."),
        SourceMetric("Original", "2.499 ден."),
        SourceMetric("Manufacturer", "Lenovo"),
        SourceMetric("Code", "CODE-42"),
        SourceMetric("Online", "Available"),
    )


def test_neptun_strategy_does_not_present_non_positive_price_as_free() -> None:
    entry = NeptunStrategy().get_entry_data(make_product(actual_price=0))

    assert entry.source_metrics[0] == SourceMetric("Price", "Ask for price")
    assert all(metric.value != "0 ден." for metric in entry.source_metrics)


def test_neptun_mkd_format_matches_retailer_grouping() -> None:
    assert format_neptun_mkd(Decimal("1234567.50")) == "1.234.567,5 ден."


def test_neptun_product_models_observed_currency_and_warranty_types() -> None:
    product = make_product()

    assert product.currency_label == "ден."
    assert product.warranty == 12


def test_neptun_strategy_interprets_winter_date_in_europe_skopje() -> None:
    entry = NeptunStrategy().get_entry_data(
        make_product(date_inserted="2026-01-21T15:44:34.97"),
    )

    assert entry.timestamp == "2026-01-21T15:44:34.970000+01:00"
    assert entry.timestamp is not None
    assert entry.timestamp.endswith("+01:00")


def test_neptun_product_rejects_offset_aware_external_date() -> None:
    with pytest.raises(ValidationError, match="DateInserted"):
        NeptunProduct.model_validate(
            product_payload(
                42,
                date_inserted="2026-07-21T15:44:34.97+03:00",
            ),
        )
