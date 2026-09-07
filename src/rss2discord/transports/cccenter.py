"""CCCenter public WooCommerce catalog strategy."""

from collections.abc import Callable
from decimal import Decimal
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.cccenter_bounds import CCCENTER_LABEL
from rss2discord.transports.cccenter_catalog import CCCenterCatalogClient
from rss2discord.transports.cccenter_models import CCCenterProduct


@final
class CCCenterStrategy(ScraperStrategy):
    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True
    max_new_entries_per_fetch = 2_000
    max_delivery_history = 10_000

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._client = CCCenterCatalogClient()
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[CCCenterProduct], str]:
        return (
            list(
                self._client.fetch_latest_products(
                    url,
                    self._is_shutdown_requested,
                ),
            ),
            CCCENTER_LABEL,
        )

    @override
    def get_entry_id(self, entry: CCCenterProduct) -> EntryId:
        return EntryId(entry.product_id)

    @override
    def get_entry_data(self, entry: CCCenterProduct) -> EntryData:
        metrics: list[SourceMetric] = []
        if (
            entry.price_status == "scalar"
            and entry.current_price is not None
            and entry.current_price > 0
        ):
            metrics.append(
                SourceMetric("Price", format_cccenter_mkd(entry.current_price)),
            )
            if (
                entry.original_price is not None
                and entry.original_price != entry.current_price
            ):
                metrics.append(
                    SourceMetric("Original", format_cccenter_mkd(entry.original_price)),
                )
        metrics.append(
            SourceMetric("Stock", "In stock" if entry.is_in_stock else "Out of stock"),
        )
        if entry.sku:
            metrics.append(SourceMetric("SKU", entry.sku))
        return EntryData(
            title=entry.name,
            link=entry.url,
            description="",
            author="",
            timestamp=None,
            image_url=entry.image_url,
            categories=entry.categories,
            source_metrics=tuple(metrics),
        )


def format_cccenter_mkd(amount: Decimal) -> str:
    canonical = canonicalize_price_amount(amount)
    whole, _, fraction = canonical.partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    fraction = fraction.rstrip("0") or "00"
    return f"{grouped},{fraction} ден."
