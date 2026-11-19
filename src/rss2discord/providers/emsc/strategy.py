"""EMSC earthquake strategy."""

from typing import final, override
from urllib.parse import quote

from rss2discord.models import EntryData, EntryId
from rss2discord.providers.emsc.bounds import EMSC_EVENT_URL, EMSC_LABEL
from rss2discord.providers.emsc.catalog import EmscCatalogClient
from rss2discord.providers.emsc.models import EarthquakeEvent
from rss2discord.transports.base import ScraperStrategy


@final
class EmscStrategy(ScraperStrategy):
    """Discover earthquakes from a configurable EMSC FDSN event query."""

    seed_existing_on_first_fetch = True

    def __init__(self) -> None:
        self._client = EmscCatalogClient()

    @override
    def fetch_entries(self, url: str) -> tuple[list[EarthquakeEvent], str]:
        return list(self._client.fetch_events(url)), EMSC_LABEL

    @override
    def get_entry_id(self, entry: EarthquakeEvent) -> EntryId:
        return EntryId(entry.event_id)

    @override
    def get_entry_data(self, entry: EarthquakeEvent) -> EntryData:
        magnitude = f"{entry.magnitude:.1f}"
        description_lines = [f"Depth: {entry.depth_km:g} km"]
        if entry.magnitude_type:
            description_lines.append(f"Magnitude type: {entry.magnitude_type}")
        if entry.author:
            description_lines.append(f"Source: {entry.author}")
        return EntryData(
            title=f"M {magnitude} — {entry.region}",
            link=EMSC_EVENT_URL.format(event_id=quote(entry.event_id, safe="")),
            description="\n".join(description_lines),
            author=entry.author,
            timestamp=entry.time,
            categories=("earthquake",),
        )
