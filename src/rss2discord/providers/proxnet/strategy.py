"""PROXNet category discovery strategy."""

from collections.abc import Callable
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.proxnet.bounds import PROXNET_LABEL
from rss2discord.providers.proxnet.catalog import ProxnetCatalogClient
from rss2discord.providers.proxnet.models import ProxnetProduct
from rss2discord.transports.base import ScraperStrategy


@final
class ProxnetStrategy(ScraperStrategy):
    """Discover newly listed products from one bounded PROXNet category."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True

    def __init__(
        self,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._client = ProxnetCatalogClient()
        self._is_shutdown_requested = is_shutdown_requested

    @override
    def fetch_entries(self, url: str) -> tuple[list[ProxnetProduct], str]:
        page = self._client.fetch_latest(url, self._is_shutdown_requested)
        return list(reversed(page.products)), page.title or PROXNET_LABEL

    @override
    def get_entry_id(self, entry: ProxnetProduct) -> EntryId:
        return EntryId(entry.product_id)

    @override
    def get_entry_data(self, entry: ProxnetProduct) -> EntryData:
        return EntryData(
            title=entry.name,
            link=entry.url,
            description=self._truncate(self._clean_html(entry.description)),
            author="",
            timestamp=entry.observed_at.isoformat(),
            image_url=entry.image_url,
            categories=(entry.category,) if entry.category else (),
            source_metrics=(SourceMetric("Price", entry.formatted_price),),
        )
