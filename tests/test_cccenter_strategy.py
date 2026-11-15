"""Entry rendering for the CCCenter discovery strategy."""

from dataclasses import replace
from decimal import Decimal

from rss2discord.models import EntryId, SourceMetric
from rss2discord.providers.cccenter.models import CCCenterProduct
from rss2discord.providers.cccenter.strategy import CCCenterStrategy

BASE = CCCenterProduct(
    product_id="https://cccenter.mk/product/alpha/",
    name="Alpha",
    url="https://cccenter.mk/product/alpha/",
    sku="ALPHA-1",
    current_price=Decimal(1200),
    original_price=Decimal(1500),
    image_url="https://cccenter.mk/alpha.jpg",
    categories=("Laptops",),
    is_in_stock=True,
    price_status="scalar",
)


def test_cccenter_strategy_uses_product_url_as_entry_id() -> None:
    assert CCCenterStrategy().get_entry_id(BASE) == EntryId(BASE.product_id)


def test_cccenter_strategy_renders_price_original_stock_and_sku() -> None:
    data = CCCenterStrategy().get_entry_data(BASE)

    assert data.title == "Alpha"
    assert data.link == BASE.url
    assert data.image_url == BASE.image_url
    assert data.categories == ("Laptops",)
    assert data.source_metrics == (
        SourceMetric("Price", "1.200,00 ден."),
        SourceMetric("Original", "1.500,00 ден."),
        SourceMetric("Stock", "In stock"),
        SourceMetric("SKU", "ALPHA-1"),
    )


def test_cccenter_strategy_omits_original_when_prices_match() -> None:
    product = replace(BASE, original_price=Decimal(1200))

    metrics = CCCenterStrategy().get_entry_data(product).source_metrics

    assert SourceMetric("Original", "1.200,00 ден.") not in metrics
    assert SourceMetric("Price", "1.200,00 ден.") in metrics


def test_cccenter_strategy_skips_price_for_variable_products() -> None:
    product = replace(BASE, price_status="variable")

    metrics = CCCenterStrategy().get_entry_data(product).source_metrics

    assert metrics == (
        SourceMetric("Stock", "In stock"),
        SourceMetric("SKU", "ALPHA-1"),
    )


def test_cccenter_strategy_skips_price_for_zero_amounts() -> None:
    product = replace(BASE, current_price=Decimal(0))

    metrics = CCCenterStrategy().get_entry_data(product).source_metrics

    assert SourceMetric("Price", "0,00 ден.") not in metrics
    assert SourceMetric("Stock", "In stock") in metrics


def test_cccenter_strategy_reports_out_of_stock_without_sku() -> None:
    product = replace(
        BASE,
        price_status="unpriced",
        current_price=None,
        original_price=None,
        is_in_stock=False,
        sku="",
    )

    metrics = CCCenterStrategy().get_entry_data(product).source_metrics

    assert metrics == (SourceMetric("Stock", "Out of stock"),)
