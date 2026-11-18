"""Normalized Time.mk article values."""

from dataclasses import dataclass
from datetime import datetime

from rss2discord.models import EntryId


@dataclass(frozen=True, slots=True)
class TimeMkArticle:
    """One aggregated Time.mk headline with its source and category."""

    entry_id: EntryId
    title: str
    link: str
    source: str
    category: str | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class TimeMkArticleDetails:
    """Open Graph enrichment read from one Time.mk article page."""

    description: str
    image_url: str | None
