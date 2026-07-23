"""Anhoch public catalog strategy."""

from __future__ import annotations

from datetime import UTC, datetime

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports.anhoch_catalog import (
    ANHOCH_LABEL,
    ANHOCH_PRODUCT_BASE_URL,
    AnhochCatalogClient,
)
from rss2discord.transports.anhoch_models import (
    AnhochImage,
    AnhochInstallments,
    AnhochMoney,
    AnhochProduct,
)
from rss2discord.transports.base import ScraperStrategy

__all__ = [
    "ANHOCH_LABEL",
    "ANHOCH_PRODUCT_BASE_URL",
    "AnhochCatalogClient",
    "AnhochImage",
    "AnhochInstallments",
    "AnhochMoney",
    "AnhochProduct",
    "AnhochStrategy",
]


class AnhochStrategy(ScraperStrategy):
    """Discover newly listed products from the public Anhoch catalog."""

    seed_existing_on_first_fetch = True

    def __init__(self) -> None:
        self._client = AnhochCatalogClient()

    def fetch_entries(self, url: str) -> tuple[list[AnhochProduct], str]:
        """Fetch a bounded window of the latest Anhoch products."""
        products = self._client.fetch_latest_products(url)
        return list(reversed(products)), ANHOCH_LABEL

    def get_entry_id(self, entry: AnhochProduct) -> EntryId:
        """Return the stable numeric Anhoch product ID."""
        return EntryId(str(entry.id))

    def get_entry_data(self, entry: AnhochProduct) -> EntryData:
        """Map a newly observed catalog product to Discord entry data."""
        metrics = [SourceMetric(label="Price", value=entry.selling_price.formatted)]
        if entry.price.formatted != entry.selling_price.formatted:
            metrics.append(SourceMetric(label="Original", value=entry.price.formatted))
        stock = str(entry.qty) if entry.is_in_stock and entry.qty is not None else "0"
        metrics.append(SourceMetric(label="Stock", value=stock))
        if entry.installments is not None:
            metrics.append(
                SourceMetric(
                    label="Installments",
                    value=f"{entry.installments.period} × {entry.installments.price.formatted}",
                ),
            )
        return EntryData(
            title=entry.name,
            link=f"{ANHOCH_PRODUCT_BASE_URL}{entry.slug}",
            description="",
            author="",
            timestamp=datetime.now(UTC).isoformat(),
            image_url=entry.base_image.path if entry.base_image is not None else None,
            source_metrics=tuple(metrics),
        )
