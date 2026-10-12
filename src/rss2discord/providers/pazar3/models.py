from dataclasses import dataclass
from datetime import datetime

from rss2discord.models import EntryId


@dataclass(frozen=True, slots=True)
class Pazar3Listing:
    entry_id: EntryId
    url: str
    title: str
    price: str
    location: str
    category: str
    activity_at: datetime
    image_url: str | None


@dataclass(frozen=True, slots=True)
class Pazar3Page:
    listings: tuple[Pazar3Listing, ...]
    organic_ids: frozenset[EntryId]
    organic_row_count: int
    result_count: int
    terminal: bool
