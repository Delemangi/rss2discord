"""Fakulteti.mk student-news strategy."""

from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.fakulteti_mk.bounds import FAKULTETI_MK_LABEL
from rss2discord.providers.fakulteti_mk.catalog import FakultetiMkCatalogClient
from rss2discord.providers.fakulteti_mk.models import FakultetiMkPost
from rss2discord.transports.base import ScraperStrategy


@final
class FakultetiMkStrategy(ScraperStrategy):
    """Discover Fakulteti.mk posts from an all-posts or category-subset URL."""

    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True
    allow_missing_timestamp_after_complete_baseline = True

    def __init__(self) -> None:
        self._client = FakultetiMkCatalogClient()

    @override
    def fetch_entries(self, url: str) -> tuple[list[FakultetiMkPost], str]:
        return list(self._client.fetch_posts(url)), FAKULTETI_MK_LABEL

    @override
    def get_entry_id(self, entry: FakultetiMkPost) -> EntryId:
        return entry.entry_id

    @override
    def get_entry_data(self, entry: FakultetiMkPost) -> EntryData:
        metrics: list[SourceMetric] = []
        if entry.comments:
            metrics.append(SourceMetric("Comments", str(entry.comments)))
        if entry.shares:
            metrics.append(SourceMetric("Shares", str(entry.shares)))
        return EntryData(
            title=entry.title,
            link=entry.link,
            description=entry.description,
            author="",
            timestamp=(
                entry.published_at.isoformat()
                if entry.published_at is not None
                else None
            ),
            image_url=entry.image_url,
            categories=(entry.category,) if entry.category else (),
            source_metrics=tuple(metrics),
        )
