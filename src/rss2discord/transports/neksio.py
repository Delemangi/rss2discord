"""Neksio public catalog strategy."""

from collections.abc import Callable
from typing import Final

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.neksio_catalog import NeksioCatalogClient
from rss2discord.transports.neksio_catalog_http import NEKSIO_LABEL, NEKSIO_ORIGIN
from rss2discord.transports.neksio_models import NeksioProduct

NEKSIO_PRODUCT_DETAILS_PATH: Final = "Product/Details/"

__all__ = ["NeksioProduct", "NeksioStrategy"]


class NeksioStrategy(ScraperStrategy):
    """Discover newly listed products from the public Neksio catalog."""

    seed_existing_on_first_fetch = True

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._client = NeksioCatalogClient()
        self._is_shutdown_requested = is_shutdown_requested

    def fetch_entries(self, url: str) -> tuple[list[NeksioProduct], str]:
        """Fetch the complete current Neksio catalog."""
        return (
            list(
                self._client.fetch_catalog(
                    url,
                    is_shutdown_requested=self._is_shutdown_requested,
                ),
            ),
            NEKSIO_LABEL,
        )

    def get_entry_id(self, entry: NeksioProduct) -> EntryId:
        """Return the stable Neksio product ID."""
        return EntryId(str(entry.product_id))

    def get_entry_data(self, entry: NeksioProduct) -> EntryData:
        """Map one observed Neksio product to ordinary entry data."""
        metrics = [SourceMetric(label="Price", value=entry.formatted_price)]
        if entry.old_formatted_price:
            metrics.append(
                SourceMetric(label="Original", value=entry.old_formatted_price),
            )
        if entry.product_code:
            metrics.append(SourceMetric(label="Product code", value=entry.product_code))
        if entry.manufacturer:
            metrics.append(SourceMetric(label="Manufacturer", value=entry.manufacturer))
        metrics.append(SourceMetric(label="Stock", value=str(entry.stock_quantity)))
        return EntryData(
            title=entry.product_name,
            link=f"{NEKSIO_ORIGIN}{NEKSIO_PRODUCT_DETAILS_PATH}{entry.product_id}",
            description="",
            author="",
            timestamp=entry.observed_at.isoformat(),
            image_url=(
                f"{NEKSIO_ORIGIN}{entry.image_path.lstrip('/')}"
                if entry.image_path
                else None
            ),
            categories=_categories(entry),
            source_metrics=tuple(metrics),
        )


def _categories(product: NeksioProduct) -> tuple[str, ...]:
    return tuple(value for value in (product.category, product.subcategory) if value)
