"""Gjirafa50 newest-product strategy."""

from collections.abc import Callable
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.gjirafa50.catalog import (
    Gjirafa50CatalogClient,
)
from rss2discord.providers.gjirafa50.models import Gjirafa50Product
from rss2discord.providers.gjirafa50.parser import GJIRAFA50_LABEL
from rss2discord.transports.base import ScraperStrategy


@final
class Gjirafa50Strategy(ScraperStrategy):
    seed_existing_on_first_fetch: bool = True
    require_entries_for_initialization: bool = True

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[Gjirafa50Product], str]:
        return (
            list(
                Gjirafa50CatalogClient().fetch_latest_products(
                    url,
                    is_shutdown_requested=self._is_shutdown_requested,
                ),
            ),
            GJIRAFA50_LABEL,
        )

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
