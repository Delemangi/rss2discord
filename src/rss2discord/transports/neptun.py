"""Neptun category product strategy."""

from decimal import Decimal
from typing import Final, final, override
from zoneinfo import ZoneInfo

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.neptun_catalog import (
    NEPTUN_WINDOW_SIZE,
    NeptunCatalogClient,
)
from rss2discord.transports.neptun_http import NEPTUN_LABEL, NEPTUN_ORIGIN
from rss2discord.transports.neptun_models import NeptunProduct

MAX_NEPTUN_DELIVERY_HISTORY: Final = 10_000
NEPTUN_TIME_ZONE: Final = "Europe/Skopje"


@final
class NeptunStrategy(ScraperStrategy):
    """Discover the newest products from one configured Neptun category."""

    seed_existing_on_first_fetch: bool = True
    require_entries_for_initialization: bool = True
    max_new_entries_per_fetch: int | None = NEPTUN_WINDOW_SIZE
    max_delivery_history: int | None = MAX_NEPTUN_DELIVERY_HISTORY

    @override
    def fetch_entries(self, url: str) -> tuple[list[NeptunProduct], str]:
        return list(NeptunCatalogClient().fetch_latest_products(url)), NEPTUN_LABEL

    @override
    def get_entry_id(self, entry: NeptunProduct) -> EntryId:
        return EntryId(str(entry.id))

    @override
    def get_entry_data(self, entry: NeptunProduct) -> EntryData:
        metrics = [
            SourceMetric(
                "Price",
                format_neptun_mkd(entry.actual_price)
                if entry.actual_price > 0
                else "Ask for price",
            ),
        ]
        if entry.actual_price > 0 and entry.regular_price != entry.actual_price:
            metrics.append(SourceMetric("Original", format_neptun_mkd(entry.regular_price)))
        metrics.extend(
            (
                SourceMetric("Manufacturer", entry.manufacturer.name),
                SourceMetric("Code", entry.code_number),
                SourceMetric(
                    "Online",
                    "Available"
                    if entry.available_online and entry.available_webshop
                    else "Unavailable",
                ),
            ),
        )
        return EntryData(
            title=entry.title,
            link=(
                f"{NEPTUN_ORIGIN}/categories/{entry.category.url.strip('/')}/"
                f"{entry.url.strip('/')}"
            ),
            description="",
            author="",
            timestamp=entry.date_inserted.replace(
                tzinfo=ZoneInfo(NEPTUN_TIME_ZONE),
            ).isoformat(),
            image_url=(
                f"{NEPTUN_ORIGIN}/{entry.thumbnail.lstrip('/')}"
                if entry.thumbnail
                else None
            ),
            categories=(entry.category.name,),
            source_metrics=tuple(metrics),
        )


def format_neptun_mkd(amount: int | Decimal) -> str:
    canonical_amount = canonicalize_price_amount(Decimal(amount))
    whole_digits, _, fractional_digits = canonical_amount.partition(".")
    grouped_whole_digits = f"{int(whole_digits):,}".replace(",", ".")
    significant_fractional_digits = fractional_digits.rstrip("0")
    if significant_fractional_digits:
        return f"{grouped_whole_digits},{significant_fractional_digits} ден."
    return f"{grouped_whole_digits} ден."
