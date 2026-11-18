"""Time.mk aggregated Macedonian news strategy."""

from typing import Final, final, override

from rss2discord.models import EntryData, EntryId
from rss2discord.providers.time_mk.bounds import TIME_MK_LABEL
from rss2discord.providers.time_mk.catalog import TimeMkCatalogClient
from rss2discord.providers.time_mk.models import (
    TimeMkArticle,
    TimeMkArticleDetails,
)
from rss2discord.transports.base import ScraperStrategy

MAX_TIME_MK_DETAIL_CACHE: Final = 256


@final
class TimeMkStrategy(ScraperStrategy):
    """Discover headlines from a configurable Time.mk category subset."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True

    def __init__(self) -> None:
        self._client = TimeMkCatalogClient()
        self._details_cache: dict[str, TimeMkArticleDetails | None] = {}

    @override
    def fetch_entries(self, url: str) -> tuple[list[TimeMkArticle], str]:
        return list(self._client.fetch_articles(url)), TIME_MK_LABEL

    @override
    def get_entry_id(self, entry: TimeMkArticle) -> EntryId:
        return entry.entry_id

    @override
    def get_entry_data(self, entry: TimeMkArticle) -> EntryData:
        details = self._details_for(entry.link)
        return EntryData(
            title=entry.title,
            link=entry.link,
            description=details.description if details is not None else "",
            author=entry.source,
            timestamp=(
                entry.published_at.isoformat()
                if entry.published_at is not None
                else None
            ),
            image_url=details.image_url if details is not None else None,
            categories=(entry.category,) if entry.category else (),
        )

    def _details_for(self, link: str) -> TimeMkArticleDetails | None:
        if link not in self._details_cache:
            if len(self._details_cache) >= MAX_TIME_MK_DETAIL_CACHE:
                self._details_cache.clear()
            self._details_cache[link] = self._client.fetch_details(link)
        return self._details_cache[link]
