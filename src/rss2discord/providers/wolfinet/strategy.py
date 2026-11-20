"""Wolfinet shop discovery strategy."""

from collections.abc import Callable
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.wolfinet.bounds import WOLFINET_LABEL
from rss2discord.providers.wolfinet.catalog import WolfinetCatalogClient
from rss2discord.providers.wolfinet.models import WolfinetProduct
from rss2discord.transports.base import ScraperStrategy


@final
class WolfinetStrategy(ScraperStrategy):
    """Discover newly listed products from the bounded Wolfinet shop."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._client = WolfinetCatalogClient()
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[WolfinetProduct], str]:
        products = self._client.fetch_latest(url, self._is_shutdown_requested)
        return list(reversed(products)), WOLFINET_LABEL

    @override
    def get_entry_id(self, entry: WolfinetProduct) -> EntryId:
        return EntryId(entry.product_id)

    @override
    def get_entry_data(self, entry: WolfinetProduct) -> EntryData:
        return EntryData(
            title=entry.name,
            link=entry.url,
            description=self._truncate(self._clean_html(entry.description)),
            author="",
            timestamp=entry.observed_at.isoformat(),
            image_url=entry.image_url,
            categories=(),
            source_metrics=(SourceMetric("Price", entry.formatted_price),),
        )
