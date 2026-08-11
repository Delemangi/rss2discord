"""Hivetec public WooCommerce product strategy."""

from collections.abc import Callable
from decimal import Decimal
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.hivetec_bounds import HIVETEC_LABEL, HIVETEC_WINDOW_SIZE
from rss2discord.transports.hivetec_catalog import HivetecCatalogClient
from rss2discord.transports.hivetec_models import (
    HivetecDiscoveryProduct,
    HivetecProduct,
)


@final
class HivetecStrategy(ScraperStrategy):
    """Discover newly published products from Hivetec's public Store API."""

    seed_existing_on_first_fetch: bool = True
    require_entries_for_initialization: bool = True
    max_new_entries_per_fetch: int | None = HIVETEC_WINDOW_SIZE
    max_delivery_history: int | None = 10_000

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[HivetecDiscoveryProduct], str]:
        return (
            list(
                HivetecCatalogClient().fetch_latest_products(
                    url,
                    self._is_shutdown_requested,
                ),
            ),
            HIVETEC_LABEL,
        )

    @override
    def get_entry_id(self, entry: HivetecDiscoveryProduct) -> EntryId:
        return EntryId(str(entry.product.id))

    @override
    def get_entry_data(self, entry: HivetecDiscoveryProduct) -> EntryData:
        product = entry.product
        return EntryData(
            title=product.name,
            link=product.permalink,
            description="",
            author="",
            timestamp=entry.published_at.isoformat(),
            image_url=product.image_url,
            categories=tuple(category.name for category in product.categories),
            source_metrics=hivetec_product_metrics(product),
        )


def hivetec_product_metrics(
    product: HivetecProduct,
    *,
    previous_price: str | None = None,
) -> tuple[SourceMetric, ...]:
    metrics = [SourceMetric("Price", format_hivetec_mkd(product.prices.current_amount))]
    if previous_price is not None:
        metrics.append(SourceMetric("Previous", previous_price, prior=True))
    if product.prices.regular_amount != product.prices.current_amount:
        metrics.append(
            SourceMetric("Original", format_hivetec_mkd(product.prices.regular_amount)),
        )
    metrics.append(
        SourceMetric("Stock", "In stock" if product.is_in_stock else "Out of stock"),
    )
    if product.sku:
        metrics.append(SourceMetric("SKU", product.sku))
    return tuple(metrics)


def format_hivetec_mkd(amount: Decimal) -> str:
    canonical = canonicalize_price_amount(amount)
    whole_digits, _, fractional_digits = canonical.partition(".")
    grouped = f"{int(whole_digits):,}".replace(",", ".")
    significant_fraction = fractional_digits.rstrip("0")
    if significant_fraction:
        return f"{grouped},{significant_fraction} ден."
    return f"{grouped} ден."
