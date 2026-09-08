"""Technomarket category product strategy."""

from collections.abc import Callable
from decimal import Decimal
from typing import Final, final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.technomarket_bounds import (
    TECHNOMARKET_DISCOVERY_WINDOW,
    TECHNOMARKET_LABEL,
)
from rss2discord.transports.technomarket_catalog import TechnomarketCatalogClient
from rss2discord.transports.technomarket_models import TechnomarketProduct

MAX_TECHNOMARKET_DELIVERY_HISTORY: Final = 10_000


@final
class TechnomarketStrategy(ScraperStrategy):
    """Discover the newest bounded window from one Technomarket category."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True
    max_new_entries_per_fetch: int | None = TECHNOMARKET_DISCOVERY_WINDOW
    max_delivery_history: int | None = MAX_TECHNOMARKET_DELIVERY_HISTORY

    def __init__(
        self, is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[TechnomarketProduct], str]:
        return (
            list(
                TechnomarketCatalogClient().fetch_latest_products(
                    url,
                    is_shutdown_requested=self._is_shutdown_requested,
                ),
            ),
            TECHNOMARKET_LABEL,
        )

    @override
    def get_entry_id(self, entry: TechnomarketProduct) -> EntryId:
        return EntryId(entry.product_id)

    @override
    def get_entry_data(self, entry: TechnomarketProduct) -> EntryData:
        effective = entry.effective_price
        metrics = [
            SourceMetric(
                "Price",
                format_technomarket_mkd(effective)
                if effective is not None and effective > 0
                else "Ask for price",
            ),
        ]
        if (
            effective is not None
            and effective > 0
            and entry.regular_price is not None
            and entry.regular_price != effective
        ):
            metrics.append(
                SourceMetric("Original", format_technomarket_mkd(entry.regular_price)),
            )
        if entry.manufacturer:
            metrics.append(SourceMetric("Manufacturer", entry.manufacturer))
        metrics.extend(
            SourceMetric("Category", category) for category in entry.categories
        )
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


def format_technomarket_mkd(amount: Decimal) -> str:
    canonical = canonicalize_price_amount(amount)
    whole, _, fraction = canonical.partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    fraction = fraction.rstrip("0")
    return f"{grouped},{fraction} ден." if fraction else f"{grouped} ден."
