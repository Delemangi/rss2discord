"""RSS feed scraping strategy."""

import logging
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

import feedparser
import requests

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

    def get_entry_id(self, entry: Any) -> str:  # noqa: ANN401
        """Get unique identifier for an RSS entry."""
        if hasattr(entry, "id"):
            return entry.id
        if hasattr(entry, "link"):
            return entry.link
        if hasattr(entry, "title"):
            return entry.title
        return str(hash(str(entry)))

    def get_entry_data(self, entry: Any) -> dict[str, Any]:  # noqa: ANN401
        """Extract data from an RSS entry."""
        title = unescape(entry.get("title", "No Title"))
        link = entry.get("link", "")
        author = entry.get("author", "")

        description = entry.get("summary", entry.get("description", ""))
        description = self._clean_rss_description(description)
        description = self._truncate(description)

        return {
            "title": title,
            "link": link,
            "description": description,
            "author": author,
            "timestamp": self._get_timestamp(entry),
        }

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
    def _get_timestamp(entry: Any) -> str:  # noqa: ANN401
        """Get ISO timestamp from RSS entry."""
        try:
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                dt = datetime(*entry.published_parsed[:6], tzinfo=UTC)  # type: ignore[misc]
                return dt.isoformat()
            if hasattr(entry, "updated_parsed") and entry.updated_parsed:
                dt = datetime(*entry.updated_parsed[:6], tzinfo=UTC)  # type: ignore[misc]
                return dt.isoformat()
        except Exception:
            logger.debug("Could not parse timestamp from entry")
        return datetime.now(UTC).isoformat()
