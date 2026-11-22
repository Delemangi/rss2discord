"""Hetzner Cloud EU availability discovery summary."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import final, override

from rss2discord.models import EntryData, EntryId
from rss2discord.providers.hetzner_cloud.bounds import (
    HETZNER_CLOUD_CONSOLE_URL,
    HETZNER_CLOUD_LABEL,
)
from rss2discord.transports.base import ScraperStrategy


@dataclass(frozen=True, slots=True)
class HetznerCloudSummary:
    """Stable placeholder entry for a feed driven by its price monitor."""


@final
class HetznerCloudStrategy(ScraperStrategy):
    """Keep the ordinary feed healthy while availability alerts are price-driven."""

    seed_existing_on_first_fetch = True

    @override
    def fetch_entries(self, url: str) -> tuple[list[HetznerCloudSummary], str]:
        del url
        return [HetznerCloudSummary()], HETZNER_CLOUD_LABEL

    @override
    def get_entry_id(self, entry: HetznerCloudSummary) -> EntryId:
        del entry
        return EntryId("hetzner-cloud:summary")

    @override
    def get_entry_data(self, entry: HetznerCloudSummary) -> EntryData:
        del entry
        return EntryData(
            title="Hetzner Cloud availability",
            link=HETZNER_CLOUD_CONSOLE_URL,
            description=(
                "EU server-type availability. Restock alerts are handled by "
                "the price monitor."
            ),
            author=HETZNER_CLOUD_LABEL,
            timestamp=datetime.now(UTC).isoformat(),
            categories=("cloud",),
        )
