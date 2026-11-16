"""Ananas public Algolia catalog strategy."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.providers.ananas.bounds import ANANAS_LABEL
from rss2discord.providers.ananas.catalog import AnanasCatalogClient
from rss2discord.providers.ananas.models import AnanasProduct
from rss2discord.transports.base import ScraperStrategy


@final
class AnanasStrategy(ScraperStrategy):
    """Discover new Ananas products from a bounded first-published window."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._client = AnanasCatalogClient()
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[AnanasProduct], str]:
        return (
            list(
                self._client.fetch_latest_products(
                    url,
                    self._is_shutdown_requested,
                ),
            ),
            ANANAS_LABEL,
        )

    @override
    def get_entry_id(self, entry: AnanasProduct) -> EntryId:
        return EntryId(entry.product_id)

    @override
    def get_entry_data(self, entry: AnanasProduct) -> EntryData:
        metrics: list[SourceMetric] = [
            SourceMetric("Price", format_ananas_mkd(entry.price)),
        ]
        if entry.base_price is not None and entry.base_price != entry.price:
            metrics.append(
                SourceMetric("Original", format_ananas_mkd(entry.base_price)),
            )
        metrics.append(
            SourceMetric("Stock", "In stock" if entry.in_stock else "Out of stock"),
        )
        metrics.append(SourceMetric("Merchant", entry.merchant))
        return EntryData(
            title=entry.name,
            link=entry.url,
            description="",
            author="",
            timestamp=format_ananas_timestamp(entry.first_published_ms),
            image_url=entry.image_url,
            categories=entry.categories,
            source_metrics=tuple(metrics),
        )


def format_ananas_mkd(amount: Decimal) -> str:
    """Format whole MKD amounts like the source, for example ``7.403 ДЕН``."""
    canonical = canonicalize_price_amount(amount)
    whole, _, fraction = canonical.partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    significant_fraction = fraction.rstrip("0")
    if significant_fraction:
        return f"{grouped},{significant_fraction} ДЕН"
    return f"{grouped} ДЕН"


def format_ananas_timestamp(first_published_ms: int | None) -> str | None:
    """Convert an Ananas epoch-millisecond timestamp to ISO UTC text."""
    if first_published_ms is None:
        return None
    seconds, milliseconds = divmod(first_published_ms, 1000)
    try:
        return (
            datetime.fromtimestamp(seconds, tz=UTC)
            + timedelta(milliseconds=milliseconds)
        ).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
