"""Time.mk aggregated Macedonian news strategy."""

from typing import final, override

from rss2discord.models import EntryData, EntryId
from rss2discord.providers.time_mk.bounds import TIME_MK_LABEL
from rss2discord.providers.time_mk.catalog import TimeMkCatalogClient
from rss2discord.providers.time_mk.models import TimeMkArticle
from rss2discord.transports.base import ScraperStrategy


@final
class TimeMkStrategy(ScraperStrategy):
    """Discover headlines from a configurable Time.mk category subset."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True

    @override
    def fetch_entries(self, url: str) -> tuple[list[TimeMkArticle], str]:
        return list(TimeMkCatalogClient().fetch_articles(url)), TIME_MK_LABEL

    @override
    def get_entry_id(self, entry: TimeMkArticle) -> EntryId:
        return entry.entry_id

    @override
    def get_entry_data(self, entry: TimeMkArticle) -> EntryData:
        return EntryData(
            title=entry.title,
            link=entry.link,
            description="",
            author=entry.source,
            timestamp=(
                entry.published_at.isoformat()
                if entry.published_at is not None
                else None
            ),
            categories=(entry.category,) if entry.category else (),
        )
