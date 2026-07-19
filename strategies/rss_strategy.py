"""RSS feed scraping strategy."""

import logging
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

import feedparser
import requests

from models import EntryData, EntryId

from .base import ScraperStrategy

logger = logging.getLogger(__name__)


class RSSStrategy(ScraperStrategy):
    """Strategy for scraping RSS/Atom feeds."""

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        """Fetch entries from an RSS feed."""
        response = requests.get(
            url,
            headers={"User-Agent": feedparser.USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)

        if feed.bozo and not feed.entries:
            error_msg = f"Error parsing RSS feed: {feed.bozo_exception}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        feed_title = str(getattr(feed.feed, "title", "RSS Feed"))
        return feed.entries[::-1], feed_title

    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        """Get unique identifier for an RSS entry."""
        for field in ("id", "link"):
            raw_value = entry.get(field)
            if raw_value is not None:
                value = str(raw_value).strip()
                if value:
                    return EntryId(value)
        return None

    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        """Extract data from an RSS entry."""
        title = unescape(str(entry.get("title", "No Title")))
        link = str(entry.get("link", ""))
        author = str(entry.get("author", ""))

        description = str(entry.get("summary", entry.get("description", "")))
        description = self._clean_rss_description(description)
        description = self._truncate(description)

        return EntryData(
            title=title,
            link=link,
            description=description,
            author=author,
            timestamp=self._get_timestamp(entry),
        )

    def _clean_rss_description(self, text: str) -> str:
        """Clean HTML and Reddit-specific markup from RSS description."""
        text = self._clean_html(text)
        text = re.sub(
            r"\s*submitted by\s*/u/\S+.*$",
            "",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        text = re.sub(r"\[link\]|\[comments\]", "", text, flags=re.IGNORECASE)
        return text.strip()

    @staticmethod
    def _get_timestamp(entry: Any) -> str | None:  # noqa: ANN401
        """Get ISO timestamp from RSS entry."""
        for field in ("published_parsed", "updated_parsed"):
            parsed_time = entry.get(field)
            if parsed_time is None:
                continue
            try:
                parsed_datetime = datetime(
                    parsed_time.tm_year,
                    parsed_time.tm_mon,
                    parsed_time.tm_mday,
                    parsed_time.tm_hour,
                    parsed_time.tm_min,
                    parsed_time.tm_sec,
                    tzinfo=UTC,
                )
            except (AttributeError, ValueError):
                continue
            return parsed_datetime.isoformat()
        return None
