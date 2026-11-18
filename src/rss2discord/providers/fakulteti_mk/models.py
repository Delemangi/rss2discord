"""Normalized Fakulteti.mk post values."""

from dataclasses import dataclass
from datetime import datetime

from rss2discord.models import EntryId


@dataclass(frozen=True, slots=True)
class FakultetiMkPost:
    """One Fakulteti.mk post with its category and social counters."""

    entry_id: EntryId
    title: str
    link: str
    description: str
    image_url: str | None
    published_at: datetime | None
    category: str | None
    comments: int
    shares: int
