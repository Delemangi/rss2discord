"""Gjirafa50 newest-product strategy."""

from decimal import Decimal
from typing import Final, final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.transports.base import ScraperStrategy
from rss2discord.transports.gjirafa50_catalog import (
    GJIRAFA50_WINDOW_SIZE,
    Gjirafa50CatalogClient,
)
from rss2discord.transports.gjirafa50_models import Gjirafa50Product
from rss2discord.transports.gjirafa50_parser import GJIRAFA50_LABEL

MAX_GJIRAFA50_DELIVERY_HISTORY: Final = 100_000


@final
class Gjirafa50Strategy(ScraperStrategy):
    seed_existing_on_first_fetch: bool = True
    require_entries_for_initialization: bool = True
    max_new_entries_per_fetch: int | None = GJIRAFA50_WINDOW_SIZE
    max_delivery_history: int | None = MAX_GJIRAFA50_DELIVERY_HISTORY

    @override
    def fetch_entries(self, url: str) -> tuple[list[Gjirafa50Product], str]:
        return list(Gjirafa50CatalogClient().fetch_latest_products(url)), GJIRAFA50_LABEL

    @override
    def get_entry_id(self, entry: Gjirafa50Product) -> EntryId:
        return EntryId(str(entry.id))

    @override
    def get_entry_data(self, entry: Gjirafa50Product) -> EntryData:
        return EntryData(
            title=entry.title,
            link=entry.link,
            description="",
            author="",
            timestamp=entry.observed_at.isoformat(),
            image_url=entry.image_url,
            source_metrics=(SourceMetric("Price", entry.formatted_price),),
        )


def format_gjirafa50_mkd(amount: Decimal) -> str:
    canonical_amount = canonicalize_price_amount(amount)
    whole_digits, _, fractional_digits = canonical_amount.partition(".")
    grouped = f"{int(whole_digits):,}".replace(",", ".")
    fraction = fractional_digits.rstrip("0")
    return f"{grouped},{fraction} MKD." if fraction else f"{grouped} MKD."
